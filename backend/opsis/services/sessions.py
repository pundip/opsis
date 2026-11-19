"""Browser sessions: a random id in the opsis_session cookie, a row in `sessions`.

Only the SHA-256 of the cookie value is stored, so a database dump does not
contain usable sessions. A session ends after SESSION_IDLE_HOURS without a
request, after SESSION_MAX_DAYS in any case, on sign-out, and when the user's
password changes or the user is deactivated.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession
from starlette.responses import Response

from opsis.auth.cookies import SESSION_COOKIE, cookie_attrs
from opsis.db.models import Session, User
from opsis.settings import Settings

# last_seen_at is written at most this often, so reading pages does not write every time.
TOUCH_EVERY = timedelta(minutes=1)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create(
    db: DbSession, settings: Settings, user: User, *, ip: str | None, user_agent: str | None
) -> str:
    """Start a session and return the cookie value. The caller commits."""
    value = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        Session(
            id=_hash(value),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.session_max_days),
            ip=(ip or "")[:45] or None,
            user_agent=(user_agent or "")[:400] or None,
        )
    )
    user.last_login_at = now
    return value


def resolve(db: DbSession, settings: Settings, values: list[str]) -> tuple[User, Session] | None:
    """The user and session behind a cookie, if it is still valid."""
    now = datetime.now(UTC)
    idle = timedelta(hours=settings.session_idle_hours)
    for value in values:
        row = db.get(Session, _hash(value))
        if row is None or row.revoked_at is not None or row.expires_at <= now:
            continue
        if row.last_seen_at + idle <= now:
            continue
        user = db.get(User, row.user_id)
        if user is None or not user.is_active:
            continue
        if now - row.last_seen_at > TOUCH_EVERY:
            row.last_seen_at = now
            db.commit()
        return user, row
    return None


def revoke(db: DbSession, session_id: str) -> None:
    db.execute(
        update(Session)
        .where(Session.id == session_id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def revoke_all(db: DbSession, user_id: uuid.UUID, *, keep: str | None = None) -> int:
    """End every session of a user, except `keep`. The caller commits."""
    query = update(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
    if keep:
        query = query.where(Session.id != keep)
    result = db.execute(query.values(revoked_at=datetime.now(UTC)))
    return int(getattr(result, "rowcount", 0) or 0)


def active_count(db: DbSession, user_id: uuid.UUID) -> int:
    now = datetime.now(UTC)
    rows = db.scalars(
        select(Session.id).where(
            Session.user_id == user_id, Session.revoked_at.is_(None), Session.expires_at > now
        )
    ).all()
    return len(rows)


def set_cookie(response: Response, settings: Settings, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=int(settings.session_max_days * 86400),
        **cookie_attrs(settings, http_only=True),
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    attrs = cookie_attrs(settings, http_only=True)
    response.delete_cookie(
        SESSION_COOKIE,
        path=attrs["path"],
        secure=attrs["secure"],
        httponly=True,
        samesite=attrs["samesite"],
    )
