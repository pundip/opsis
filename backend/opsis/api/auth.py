"""Sign-in routes (AUTH_MODE=local or full only; not registered otherwise)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import client_ip, get_db, get_settings, signed_in_person
from opsis.api.errors import ApiError
from opsis.auth import cookies, google
from opsis.auth.principal import Principal
from opsis.db.models import User
from opsis.services import accounts, audit, sessions, users
from opsis.settings import Settings

router = APIRouter(prefix="/api/auth")


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1000)


class PasswordIn(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=1000)
    newPassword: str = Field(min_length=1, max_length=1000)


class LinkIn(BaseModel):
    password: str = Field(min_length=1, max_length=1000)
    displayName: str | None = Field(default=None, max_length=200)


def _start_session(
    db: Session, settings: Settings, request: Request, response: Response, user: User, method: str
) -> None:
    value = sessions.create(
        db, settings, user, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    audit.record(
        db,
        action="auth.login",
        actor=users.to_principal(user, method),
        target_type="user",
        target_id=str(user.id),
        detail={"method": method},
        ip=client_ip(request),
    )
    db.commit()
    sessions.set_cookie(response, settings, value)


@router.post("/login")
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    user = accounts.sign_in_with_password(db, body.email, body.password, client_ip(request))
    _start_session(db, settings, request, response, user, "password")
    return {"signedIn": True, "mustChangePassword": user.must_change_password}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(signed_in_person),
) -> dict[str, Any]:
    if principal.session_id:
        sessions.revoke(db, principal.session_id)
        audit.record(db, action="auth.logout", actor=principal, ip=client_ip(request))
        db.commit()
    sessions.clear_cookie(response, settings)
    return {"signedIn": False}


@router.post("/logout-everywhere")
def logout_everywhere(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(signed_in_person),
) -> dict[str, Any]:
    ended = sessions.revoke_all(db, principal.user_id)
    audit.record(
        db,
        action="auth.logout_everywhere",
        actor=principal,
        detail={"sessions": ended},
        ip=client_ip(request),
    )
    db.commit()
    sessions.clear_cookie(response, settings)
    return {"signedIn": False, "sessionsEnded": ended}


@router.post("/password")
def change_password(
    body: PasswordIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(signed_in_person),
) -> dict[str, Any]:
    accounts.change_password(db, principal, body.currentPassword, body.newPassword)
    return {"changed": True}


@router.get("/links/{token}")
def peek_link(token: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return accounts.peek_link(db, token)


@router.post("/links/{token}")
def use_link(
    token: str,
    body: LinkIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    user = accounts.use_link(db, token, body.password, body.displayName)
    _start_session(db, settings, request, response, user, "link")
    return {"signedIn": True}


# ---- Google -------------------------------------------------------------------------


def _safe_next(value: str | None) -> str:
    """Only paths inside the app: never another site (an open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:500]


@router.get("/google/start")
def google_start(next: str | None = None, settings: Settings = Depends(get_settings)) -> Response:
    if settings.auth_mode != "full":
        raise ApiError(404, "not_found", "Not found.")
    try:
        url, flow = google.start(settings, _safe_next(next))
    except google.GoogleSignInError as exc:
        return RedirectResponse(f"{settings.base_path}/login?error={exc.code}", status_code=303)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        google.FLOW_COOKIE,
        flow,
        max_age=google.FLOW_MAX_AGE,
        **cookies.cookie_attrs(settings, http_only=True),
    )
    return response


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if settings.auth_mode != "full":
        raise ApiError(404, "not_found", "Not found.")
    base = settings.base_path

    def back_to_login(code_: str) -> Response:
        failed = RedirectResponse(f"{base}/login?error={code_}", status_code=303)
        failed.delete_cookie(google.FLOW_COOKIE, path=settings.cookie_path)
        return failed

    if error or not code or not state:
        return back_to_login("google_cancelled" if error == "access_denied" else "google_failed")
    try:
        claims, next_path = google.finish(
            settings,
            code=code,
            state=state,
            flow_cookies=cookies.cookie_values(request, google.FLOW_COOKIE),
        )
        user = accounts.sign_in_with_google(db, settings, claims, client_ip(request))
    except google.GoogleSignInError as exc:
        return back_to_login(exc.code)
    response = RedirectResponse(f"{base}{_safe_next(next_path)}", status_code=303)
    _start_session(db, settings, request, response, user, "google")
    response.delete_cookie(google.FLOW_COOKIE, path=settings.cookie_path)
    return response
