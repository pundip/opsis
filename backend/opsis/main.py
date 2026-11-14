"""FastAPI application factory.

Run with: uvicorn opsis.main:create_app --factory

Everything is mounted under settings.base_path (brief 3.1): "" locally,
"/opsis" behind the shared reverse proxy. The proxy forwards the path
unchanged, so the app owns the prefix and anything outside it is a 404.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opsis import __version__
from opsis.api import ingestion, semantic, system
from opsis.api.errors import ApiError, install_error_handlers
from opsis.auth import cookies
from opsis.jobs.worker import InlineRunner
from opsis.query.duckdb_engine import DuckDBEngine, ResultCache
from opsis.services import users
from opsis.services.query import QueryRuntime
from opsis.settings import Settings, load_settings

log = logging.getLogger("opsis")

# No inline scripts: the injected config is a JSON data block, not executable.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
BACKEND_PREFIXES = ("api/", "mcp/", "healthz")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    base = settings.base_path
    engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.auth_mode == "none":
            with session_factory() as db:
                users.ensure_builtin_admin(db)
        log.info(
            "%s %s started: deploy_mode=%s auth_mode=%s job_runner=%s base_path=%r",
            settings.app_name,
            __version__,
            settings.deploy_mode,
            settings.auth_mode,
            settings.job_runner,
            base,
        )
        runner = InlineRunner(settings) if settings.job_runner == "inline" else None
        if runner is not None:
            runner.start()
        try:
            yield
        finally:
            if runner is not None:
                runner.stop()
            engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=f"{base}/api/docs",
        openapi_url=f"{base}/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.query_runtime = QueryRuntime(
        engine=DuckDBEngine(
            memory_limit_mb=settings.query_memory_limit_mb,
            threads=settings.query_threads,
            temp_dir=settings.data_dir / "tmp" / "duckdb",
        ),
        cache=ResultCache(settings.query_cache_entries),
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        inside = path == base or path.startswith(f"{base}/")
        if (
            request.method in cookies.UNSAFE_METHODS
            and path.startswith(f"{base}/api/")
            and not cookies.csrf_ok(request)
        ):
            response: Response = JSONResponse(
                {
                    "error": {
                        "code": "csrf_failed",
                        "message": "Missing or invalid CSRF token. Reload the page and try again.",
                        "details": [],
                    }
                },
                status_code=403,
            )
        else:
            response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        if not path.startswith(f"{base}/api/docs"):
            headers.setdefault("Content-Security-Policy", CSP)
        # No HSTS: it applies to the whole domain, so it is the proxy's decision.
        if inside:
            cookies.ensure_csrf_cookie(request, response, settings)
        return response

    api = APIRouter(prefix=base)
    api.include_router(system.health_router)
    api.include_router(system.router)
    register_feature_routers(api)
    app.include_router(api)
    _mount_frontend(app, settings)
    return app


def register_feature_routers(api: APIRouter) -> None:
    """Every feature router is included here, under the base path."""
    api.include_router(ingestion.router)
    api.include_router(semantic.router)


def render_index(settings: Settings) -> str | None:
    """index.html with the base href and runtime config injected.

    The same image runs at "/" and at "/opsis", so the frontend is built with
    relative URLs and learns its base path here, at request time.
    """
    index = settings.static_dir / "index.html"
    if not index.is_file():
        return None
    config = {
        "basePath": settings.base_path,
        "appName": settings.app_name,
        "version": __version__,
    }
    payload = json.dumps(config).replace("<", "\\u003c")
    inject = (
        f'<base href="{settings.base_path}/" />\n'
        f'    <script id="opsis-config" type="application/json">{payload}</script>'
    )
    return index.read_text(encoding="utf-8").replace("<head>", f"<head>\n    {inject}", 1)


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built single-page app. The fallback is active only inside base_path."""
    base = settings.base_path
    static_dir = settings.static_dir.resolve()
    index_html = render_index(settings)
    if (static_dir / "assets").is_dir():
        app.mount(f"{base}/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    if base:

        @app.get(base, include_in_schema=False)
        def to_slash() -> Response:
            return RedirectResponse(f"{base}/", status_code=308)

    @app.get(base + "/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith(BACKEND_PREFIXES) or path in ("api", "mcp"):
            raise ApiError(404, "not_found", "Not found.")
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static_dir):
            return FileResponse(candidate)
        if index_html is not None:
            return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})
        raise ApiError(404, "not_found", "The frontend is not built in this image.")
