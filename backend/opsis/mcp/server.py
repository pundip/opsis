"""The MCP endpoint at {BASE_PATH}/mcp (Streamable HTTP).

Stateless, with plain JSON responses: every request stands alone, which is
the simplest thing to carry through a reverse proxy and needs no session
store. Tool calls are synchronous service code, so they run in a worker
thread and never block the event loop.

Authentication happens here, before the MCP SDK sees the request:
- MCP_AUTH=none (loopback only): calls run as the built-in admin.
- MCP_AUTH=token: `Authorization: Bearer <personal access token>`; calls run
  as the token's owner, limited by the token's scopes.
A request from a browser page on another origin is refused.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
import mcp_types as types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from opsis import __version__
from opsis.auth.principal import Principal
from opsis.mcp import guide, tools
from opsis.services import dashboards, tokens, users
from opsis.services.query import QueryRuntime
from opsis.settings import Settings, is_loopback

log = logging.getLogger("opsis.mcp")

PRINCIPAL_KEY = "opsis.principal"
RESOURCE_PREFIX = "opsis://"
SCHEMA_RESOURCES = {f"schema/{name}": file for name, file in tools.SCHEMAS.items()}


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class OpsisMcp:
    """Builds the MCP server and the ASGI endpoint that guards it."""

    def __init__(
        self, settings: Settings, session_factory: sessionmaker[Session], runtime: QueryRuntime
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.runtime = runtime
        self.server: Server[Any] = Server(
            "opsis",
            version=__version__,
            title=settings.app_name,
            instructions=guide.INSTRUCTIONS,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_read_resource=self._read_resource,
            on_list_prompts=self._list_prompts,
            on_get_prompt=self._get_prompt,
        )
        self.manager = StreamableHTTPSessionManager(
            app=self.server, json_response=True, stateless=True
        )
        public = urlsplit(settings.public_base_url)
        self._public_origin = f"{public.scheme}://{public.netloc}"

    # ---- HTTP: who is calling -------------------------------------------------------

    def _authenticate(self, authorization: str | None) -> Principal | None:
        with self.session_factory() as db:
            if self.settings.mcp_auth == "none":
                admin = users.get_builtin_admin(db)
                if admin is None:
                    admin = users.ensure_builtin_admin(db)
                return users.to_principal(admin, "none")
            scheme, _, secret = (authorization or "").partition(" ")
            if scheme.lower() != "bearer" or not secret.strip():
                return None
            return tokens.authenticate(db, secret.strip())

    def _origin_allowed(self, origin: str | None) -> bool:
        """Command-line and desktop clients send no Origin. A browser page does,
        and only our own pages (or a local tool) may call."""
        if not origin:
            return True
        if origin == self._public_origin:
            return True
        host = urlsplit(origin).hostname or ""
        return host == "localhost" or is_loopback(host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if request.method != "POST":
            # Stateless: there is no session to end (DELETE) and no stream to listen
            # on (GET). The MCP spec says to answer 405 in that case, which also
            # keeps idle connections off the reverse proxy.
            response = _refuse(405, "method_not_allowed", "Send MCP requests with POST.")
            response.headers["Allow"] = "POST"
            await response(scope, receive, send)
            return
        if not self._origin_allowed(request.headers.get("origin")):
            await _refuse(403, "forbidden_origin", "Requests from this origin are not allowed.")(
                scope, receive, send
            )
            return
        principal = await anyio.to_thread.run_sync(
            self._authenticate, request.headers.get("authorization")
        )
        if principal is None:
            response = _refuse(
                401,
                "not_authenticated",
                "Send a personal access token: Authorization: Bearer opsis_pat_... "
                "Create one in Opsis under Settings, Access tokens.",
            )
            response.headers["WWW-Authenticate"] = 'Bearer realm="opsis"'
            await response(scope, receive, send)
            return
        scope[PRINCIPAL_KEY] = principal
        await self.manager.asgi_app(scope, receive, send)

    # ---- MCP handlers -----------------------------------------------------------------

    def _principal(self, ctx: ServerRequestContext[Any, Any]) -> Principal:
        request = ctx.request
        principal = request.scope.get(PRINCIPAL_KEY) if isinstance(request, Request) else None
        if not isinstance(principal, Principal):
            # Never fall back to anyone: a call without an identity does not run.
            raise RuntimeError("MCP request without an authenticated principal")
        return principal

    def _context(self, ctx: ServerRequestContext[Any, Any]) -> tools.ToolContext:
        return tools.ToolContext(
            settings=self.settings,
            session_factory=self.session_factory,
            runtime=self.runtime,
            principal=self._principal(ctx),
        )

    async def _list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        offered = tools.available(self.settings, self._principal(ctx))
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.name,
                    title=tool.title,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    annotations=types.ToolAnnotations(
                        title=tool.title,
                        read_only_hint=not tool.writes,
                        destructive_hint=False,
                        open_world_hint=False,
                    ),
                )
                for tool in offered
            ]
        )

    async def _call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        context = self._context(ctx)
        try:
            result = await anyio.to_thread.run_sync(
                tools.call, context, params.name, params.arguments
            )
        except tools.ToolFailure as failure:
            return types.CallToolResult(
                content=[types.TextContent(text=failure.text)], is_error=True
            )
        except Exception:
            log.exception("Tool %s failed unexpectedly", params.name)
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        text="Error (internal_error): The tool failed unexpectedly. "
                        "What to do: this is a fault in Opsis, not in your input. Tell the user; "
                        "retrying the same call is unlikely to help."
                    )
                ],
                is_error=True,
            )
        return types.CallToolResult(content=[types.TextContent(text=_text(result))])

    async def _list_resources(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        self._principal(ctx)
        resources = [
            types.Resource(
                uri=f"{RESOURCE_PREFIX}guide",
                name="guide",
                title="How to build dashboards",
                description="A short guide to specs, items, positions and patches, with examples.",
                mime_type="text/markdown",
            ),
            types.Resource(
                uri=f"{RESOURCE_PREFIX}visuals",
                name="visuals",
                title="Visual manifests",
                description="Every visual's manifest: roles, config schema and guidance.",
                mime_type="application/json",
            ),
        ]
        resources += [
            types.Resource(
                uri=f"{RESOURCE_PREFIX}{path}",
                name=path,
                title=f"JSON Schema: {path.partition('/')[2]}",
                description=f"The JSON Schema in spec/{file}.",
                mime_type="application/schema+json",
            )
            for path, file in SCHEMA_RESOURCES.items()
        ]
        return types.ListResourcesResult(resources=resources)

    def _resource_text(self, uri: str) -> tuple[str, str]:
        key = uri.removeprefix(RESOURCE_PREFIX).strip("/")
        if key == "guide":
            return guide.GUIDE, "text/markdown"
        if key == "visuals":
            manifests = list(dashboards.manifests(self.settings).values())
            return json.dumps(manifests, indent=1), "application/json"
        if key in SCHEMA_RESOURCES:
            path = self.settings.spec_dir / SCHEMA_RESOURCES[key]
            return path.read_text(encoding="utf-8"), "application/schema+json"
        known = ", ".join(f"{RESOURCE_PREFIX}{k}" for k in ("guide", "visuals", *SCHEMA_RESOURCES))
        raise ValueError(f"There is no resource {uri!r}. The resources are: {known}.")

    async def _read_resource(
        self, ctx: ServerRequestContext[Any, Any], params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        self._principal(ctx)
        uri = str(params.uri)
        text, mime = await anyio.to_thread.run_sync(self._resource_text, uri)
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type=mime, text=text)]
        )

    async def _list_prompts(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name="build_dashboard",
                    title="Build a dashboard",
                    description=(
                        "Walks through building a dashboard from one dataset: describe the data, "
                        "propose a layout, create it, fix any problems, then summarise."
                    ),
                    arguments=[
                        types.PromptArgument(
                            name="dataset",
                            description="The dataset to build from, as named in list_datasets.",
                            required=True,
                        ),
                        types.PromptArgument(
                            name="goal",
                            description="What the dashboard should show or what it should answer.",
                            required=False,
                        ),
                    ],
                )
            ]
        )

    async def _get_prompt(
        self, ctx: ServerRequestContext[Any, Any], params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        if params.name != "build_dashboard":
            raise ValueError(
                f"There is no prompt {params.name!r}. The only prompt is build_dashboard."
            )
        arguments = params.arguments or {}
        dataset = str(arguments.get("dataset") or "").strip() or "<dataset>"
        goal = str(arguments.get("goal") or "").strip()
        return types.GetPromptResult(
            description="Build a dashboard from one dataset, step by step.",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(text=guide.build_dashboard_prompt(dataset, goal)),
                )
            ],
        )


def _refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": []}}, status)
