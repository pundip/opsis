"""REST routes for the in-app assistant panel (FEATURE_ASSISTANT).

POST .../assistant/messages starts a request and streams its progress as
server-sent events. The request itself runs on its own (see assistant/runs.py),
so the stream can end without stopping it; GET .../assistant shows everything
that was stored, and whether a request is still running.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import get_settings, require
from opsis.api.sse import sse_response
from opsis.assistant import loop
from opsis.assistant.providers import Provider
from opsis.assistant.runs import Runs
from opsis.auth.principal import Permission, Principal
from opsis.db.models import AssistantConversation
from opsis.errors import ApiError
from opsis.mcp import tools
from opsis.services import access, dashboards
from opsis.services import assistant as store
from opsis.settings import Settings

router = APIRouter(prefix="/api")

edit = require(Permission.DASHBOARDS_EDIT)


def person(principal: Principal = Depends(edit)) -> Principal:
    """The assistant spends the workspace's model budget on a person's behalf in the
    editor. Scripts and agents use the MCP server, with a model of their own."""
    if principal.auth_method == "token":
        raise ApiError(
            403,
            "forbidden",
            "The assistant is for people in the editor. With an access token, use the MCP "
            "server instead.",
        )
    return principal


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    selectedItemId: str | None = Field(default=None, max_length=100)


def _editable(db: Session, dashboard_id: uuid.UUID, principal: Principal) -> None:
    dashboard = dashboards.get(db, dashboard_id)
    access.require_dashboard(db, dashboard, principal, "edit")


def _matches(conversation: AssistantConversation | None, provider: Provider) -> bool:
    """A history can only go back to the provider and model that wrote it."""
    return conversation is not None and (conversation.provider, conversation.model) == (
        provider.name,
        provider.model,
    )


def _state(
    db: Session,
    settings: Settings,
    provider: Provider,
    runs: Runs,
    conversation: AssistantConversation | None,
) -> dict[str, Any]:
    return {
        "provider": provider.name,
        "model": provider.model,
        "budgets": {
            "steps": settings.assistant_max_steps,
            "outputTokens": settings.assistant_max_output_tokens,
            "seconds": settings.assistant_timeout_seconds,
        },
        "conversation": store.describe(conversation) if conversation is not None else None,
        "entries": store.entries(db, conversation.id) if conversation is not None else [],
        "running": conversation is not None and runs.get(conversation.id) is not None,
        # The provider or model changed since: the next request starts a new conversation.
        "outdated": conversation is not None and not _matches(conversation, provider),
    }


def _machinery(request: Request) -> tuple[Provider, Runs]:
    return request.app.state.assistant_provider, request.app.state.assistant_runs


@router.get("/dashboards/{dashboard_id}/assistant")
async def get_assistant(
    dashboard_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(person),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    provider, runs = _machinery(request)

    def read() -> dict[str, Any]:
        with request.app.state.session_factory() as db:
            _editable(db, dashboard_id, principal)
            conversation = store.latest(db, dashboard_id, principal.user_id)
            return _state(db, settings, provider, runs, conversation)

    return await anyio.to_thread.run_sync(read)


@router.post("/dashboards/{dashboard_id}/assistant/conversations", status_code=201)
async def new_conversation(
    dashboard_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(person),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Start afresh. The old conversation is kept but no longer shown."""
    provider, runs = _machinery(request)

    def start() -> dict[str, Any]:
        with request.app.state.session_factory() as db:
            _editable(db, dashboard_id, principal)
            current = store.latest(db, dashboard_id, principal.user_id)
            if current is not None and runs.get(current.id) is not None:
                raise ApiError(
                    409,
                    "assistant_busy",
                    "The assistant is still working. Stop it before starting a new conversation.",
                )
            started = store.start(
                db, dashboard_id, principal.user_id, provider.name, provider.model
            )
            return _state(db, settings, provider, runs, started)

    return await anyio.to_thread.run_sync(start)


@router.post("/dashboards/{dashboard_id}/assistant/stop")
async def stop_assistant(
    dashboard_id: uuid.UUID, request: Request, principal: Principal = Depends(person)
) -> dict[str, Any]:
    _, runs = _machinery(request)

    def find() -> uuid.UUID | None:
        with request.app.state.session_factory() as db:
            _editable(db, dashboard_id, principal)
            conversation = store.latest(db, dashboard_id, principal.user_id)
            return conversation.id if conversation is not None else None

    conversation_id = await anyio.to_thread.run_sync(find)
    run = runs.get(conversation_id) if conversation_id is not None else None
    if run is not None:
        run.stop.set()  # on the event loop, where the request waits for it
    return {"stopping": run is not None}


@router.post("/dashboards/{dashboard_id}/assistant/messages")
async def send_message(
    dashboard_id: uuid.UUID,
    body: MessageIn,
    request: Request,
    principal: Principal = Depends(person),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Ask the assistant something. Streams `start`, then `entry` for everything
    stored, `delta` and `thinking` while the model writes, `step_started` when a
    tool runs, and finally `done`."""
    provider, runs = _machinery(request)
    session_factory = request.app.state.session_factory

    def prepare() -> uuid.UUID:
        with session_factory() as db:
            _editable(db, dashboard_id, principal)
            conversation = store.latest(db, dashboard_id, principal.user_id)
            if not _matches(conversation, provider):
                conversation = store.start(
                    db, dashboard_id, principal.user_id, provider.name, provider.model
                )
            assert conversation is not None
            return conversation.id

    conversation_id = await anyio.to_thread.run_sync(prepare)
    run = runs.begin(conversation_id)
    try:

        def note() -> dict[str, Any]:
            with session_factory() as db:
                return store.add_entry(
                    db,
                    conversation_id,
                    "request",
                    {"text": body.text, "selected": body.selectedItemId},
                )

        entry = await anyio.to_thread.run_sync(note)
    except BaseException:
        runs.abandon(run)
        raise
    queue = run.listen()
    ctx = tools.ToolContext(
        settings, session_factory, request.app.state.query_runtime, principal, author_type="agent"
    )
    work = loop.Request(
        run=run,
        ctx=ctx,
        provider=provider,
        conversation_id=conversation_id,
        dashboard_id=dashboard_id,
        text=body.text,
        selected=body.selectedItemId,
    )
    runs.launch(run, work.execute())

    async def events() -> AsyncIterator[tuple[str, Any]]:
        try:
            yield "start", {"conversationId": str(conversation_id), "entry": entry}
            while True:
                event, data = await queue.get()
                yield event, data
                if event == "done":
                    return
        finally:
            run.unlisten(queue)

    return sse_response(events(), settings.sse_keepalive_seconds)
