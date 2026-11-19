"""FastAPI dependencies shared by all routers: settings, DB session, principal, guards."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from opsis.api.errors import ApiError
from opsis.auth import cookies
from opsis.auth.principal import Permission, Principal
from opsis.services import sessions, tokens, users
from opsis.settings import Settings

# While a password must be changed, only these paths work (relative to the base path).
DURING_PASSWORD_CHANGE = ("/api/me", "/api/auth/password", "/api/auth/logout")

# Token scopes (brief 10.1): admin work needs `admin`, publishing needs `publish`,
# other changes need `edit`, and reading needs `read`.
ADMIN_PERMISSIONS = frozenset(
    {
        Permission.USERS_MANAGE,
        Permission.SETTINGS_MANAGE,
        Permission.AUDIT_VIEW,
        Permission.CONNECTIONS_MANAGE,
        Permission.SQL_LAB_USE,
    }
)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_principal(
    request: Request, settings: Settings = Depends(get_settings), db: Session = Depends(get_db)
) -> Principal:
    """Resolve the caller. Raises 401 when nobody is signed in.

    An Authorization header always means a personal access token (scripts, MCP);
    it never falls back to a session cookie, even when the token is wrong.
    """
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, secret = authorization.partition(" ")
        principal = tokens.authenticate(db, secret.strip()) if scheme.lower() == "bearer" else None
        if principal is None:
            raise ApiError(
                401,
                "invalid_token",
                "The access token is not valid. It may be mistyped, expired or revoked.",
            )
        return principal
    if settings.auth_mode == "none":
        admin = users.get_builtin_admin(db)
        if admin is None:
            raise ApiError(503, "not_ready", "The built-in admin has not been created yet.")
        return users.to_principal(admin, "none")
    found = sessions.resolve(db, settings, cookies.cookie_values(request, cookies.SESSION_COOKIE))
    if found is None:
        raise ApiError(401, "not_authenticated", "Sign in to continue.")
    user, session = found
    principal = users.principal_for(db, user, "session", session_id=session.id)
    if principal.must_change_password:
        path = request.url.path.removeprefix(settings.base_path)
        if path not in DURING_PASSWORD_CHANGE:
            raise ApiError(
                403, "password_change_required", "Change your password before continuing."
            )
    return principal


def scope_for(permission: Permission, method: str, read_only: bool) -> str:
    if permission in ADMIN_PERMISSIONS:
        return "admin"
    if permission is Permission.DASHBOARDS_PUBLISH:
        return "publish"
    return "read" if read_only or method in ("GET", "HEAD") else "edit"


def require(permission: Permission, *, read_only: bool = False) -> Callable[..., Principal]:
    """Dependency factory: the caller must hold `permission`, else 403.

    With an access token, the token must also have the matching scope.
    `read_only` marks a POST that changes nothing (a query, a validation).
    """

    def guard(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(permission):
            raise ApiError(
                403, "forbidden", f"Your role ({principal.role}) does not allow {permission}."
            )
        scope = scope_for(permission, request.method, read_only)
        if not principal.has_scope(scope):
            raise ApiError(
                403,
                "forbidden",
                f"This access token does not have the `{scope}` scope needed here.",
            )
        return principal

    return guard


def signed_in_person(principal: Principal = Depends(get_principal)) -> Principal:
    """For account pages (password, tokens): a person in a browser, not a token.
    A stolen token must not be able to make more tokens or change a password."""
    if principal.auth_method == "token":
        raise ApiError(403, "forbidden", "This cannot be done with an access token.")
    return principal


def require_feature(flag: str) -> Callable[[Settings], None]:
    """Dependency factory: 404 when a feature flag is off, as if the route did not exist."""

    def guard(settings: Settings = Depends(get_settings)) -> None:
        if not getattr(settings, flag):
            raise ApiError(404, "not_found", "Not found.")

    return guard


def client_ip(request: Request) -> str | None:
    """The client address. Behind TRUSTED_PROXIES uvicorn has already replaced it
    with the forwarded one; anyone else's X-Forwarded-For is ignored."""
    return request.client.host if request.client else None
