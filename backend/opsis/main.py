"""FastAPI application factory.

Run with: uvicorn opsis.main:create_app --factory
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opsis import __version__
from opsis.api import system
from opsis.api.errors import ApiError, install_error_handlers
from opsis.services import users
from opsis.settings import Settings, load_settings

log = logging.getLogger("opsis")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
BACKEND_PREFIXES = ("api/", "mcp/", "healthz")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
    session_factory = sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.auth_mode == "none":
            with session_factory() as db:
                users.ensure_builtin_admin(db)
        log.info(
            "%s %s started: deploy_mode=%s auth_mode=%s job_runner=%s",
            settings.app_name,
            __version__,
            settings.deploy_mode,
            settings.auth_mode,
            settings.job_runner,
        )
        yield
        engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    install_error_handlers(app)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/api/docs"):
            headers.setdefault("Content-Security-Policy", CSP)
        if settings.deploy_mode == "server" and settings.is_https:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.include_router(system.health_router)
    app.include_router(system.router)
    _mount_frontend(app, settings)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built single-page app, with index.html as the fallback route."""
    static_dir = settings.static_dir.resolve()
    index = static_dir / "index.html"
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith(BACKEND_PREFIXES) or path in ("api", "mcp"):
            raise ApiError(404, "not_found", "Not found.")
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static_dir):
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        raise ApiError(404, "not_found", "The frontend is not built in this image.")
