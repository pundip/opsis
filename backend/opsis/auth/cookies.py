"""Cookie names and attributes, in one place.

Opsis may share its origin with other apps (brief 3.1), so every cookie is
prefixed "opsis_" and scoped to the base path. Auth never uses localStorage.
"""

from __future__ import annotations

import secrets
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from opsis.settings import Settings

SESSION_COOKIE = "opsis_session"  # HttpOnly, set at sign-in (M6)
CSRF_COOKIE = "opsis_csrf"  # readable by the app's own JavaScript
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def cookie_attrs(settings: Settings, *, http_only: bool) -> dict[str, Any]:
    return {
        "path": settings.cookie_path,
        "secure": settings.is_https,
        "httponly": http_only,
        "samesite": "lax",
    }


def ensure_csrf_cookie(request: Request, response: Response, settings: Settings) -> None:
    if CSRF_COOKIE not in request.cookies:
        response.set_cookie(
            CSRF_COOKIE, secrets.token_urlsafe(32), **cookie_attrs(settings, http_only=False)
        )


def csrf_ok(request: Request) -> bool:
    """Double-submit check: the header must repeat the cookie.

    Another site can make a browser send our cookie, but it cannot read the
    cookie to copy it into a header. Bearer-token clients (MCP, API tokens)
    are exempt because browsers never attach those automatically.
    """
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return True
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    return bool(cookie) and secrets.compare_digest(cookie, header)
