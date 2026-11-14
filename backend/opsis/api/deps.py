"""FastAPI dependencies shared by all routers: settings, DB session, principal, guards."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from opsis.api.errors import ApiError
from opsis.auth.principal import Permission, Principal
from opsis.services import users
from opsis.settings import Settings


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_principal(
    settings: Settings = Depends(get_settings), db: Session = Depends(get_db)
) -> Principal:
    """Resolve the caller. Raises 401 when nobody is signed in."""
    if settings.auth_mode == "none":
        admin = users.get_builtin_admin(db)
        if admin is None:
            raise ApiError(503, "not_ready", "The built-in admin has not been created yet.")
        return users.to_principal(admin, "none")
    # Session cookies and personal access tokens arrive in M5 and M6.
    raise ApiError(401, "not_authenticated", "Sign in to continue.")


def require(permission: Permission) -> Callable[[Principal], Principal]:
    """Dependency factory: the caller must hold `permission`, else 403."""

    def guard(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(permission):
            raise ApiError(
                403, "forbidden", f"Your role ({principal.role}) does not allow {permission}."
            )
        return principal

    return guard


def require_feature(flag: str) -> Callable[[Settings], None]:
    """Dependency factory: 404 when a feature flag is off, as if the route did not exist."""

    def guard(settings: Settings = Depends(get_settings)) -> None:
        if not getattr(settings, flag):
            raise ApiError(404, "not_found", "Not found.")

    return guard
