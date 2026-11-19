"""Personal access tokens: how an MCP client proves who it is.

A token is shown once, when it is made. Only its SHA-256 is stored. Each has
scopes, an expiry, can be revoked, and records when it was last used. A call
made with a token runs as the token's owner, never with more than the owner's
role allows and never with more than the token's scopes allow.
"""

from __future__ import annotations

import dataclasses
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import AccessToken, User
from opsis.errors import ApiError
from opsis.services import audit, users

TOKEN_PREFIX = "opsis_pat_"
SCOPES = ("read", "edit", "publish", "admin")
MAX_DAYS = 366
MAX_TOKENS_PER_USER = 50
# last_used_at is rewritten at most this often, so a busy agent does not write on every call.
TOUCH_EVERY = timedelta(minutes=1)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def serialize(token: AccessToken) -> dict[str, Any]:
    now = datetime.now(UTC)
    status = "revoked" if token.revoked_at else "expired" if token.expires_at <= now else "active"
    return {
        "id": str(token.id),
        "name": token.name,
        "prefix": token.prefix,
        "scopes": token.scopes,
        "status": status,
        "expiresAt": token.expires_at.isoformat(),
        "lastUsedAt": token.last_used_at.isoformat() if token.last_used_at else None,
        "createdAt": token.created_at.isoformat(),
    }


def list_tokens(db: Session, owner: Principal) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(AccessToken)
        .where(AccessToken.user_id == owner.user_id)
        .order_by(AccessToken.created_at.desc())
    ).all()
    return [serialize(row) for row in rows]


def create(
    db: Session, owner: Principal, *, name: str, scopes: list[str], days: int
) -> tuple[dict[str, Any], str]:
    """Make a token. Returns its description and the token itself, which is never shown again."""
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_request", "Give the token a name.")
    chosen = [scope for scope in SCOPES if scope in set(scopes)]
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown or not chosen:
        raise ApiError(
            422, "invalid_request", f"Choose at least one scope from: {', '.join(SCOPES)}."
        )
    if not 1 <= days <= MAX_DAYS:
        raise ApiError(422, "invalid_request", f"A token can last from 1 to {MAX_DAYS} days.")
    existing = db.scalars(select(AccessToken.id).where(AccessToken.user_id == owner.user_id)).all()
    if len(existing) >= MAX_TOKENS_PER_USER:
        raise ApiError(
            409, "too_many_tokens", "You have too many tokens. Delete some that you no longer use."
        )
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = AccessToken(
        user_id=owner.user_id,
        name=name[:100],
        token_hash=_hash(secret),
        prefix=secret[: len(TOKEN_PREFIX) + 6],
        scopes=chosen,
        expires_at=datetime.now(UTC) + timedelta(days=days),
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        action="token.created",
        actor=owner,
        target_type="access_token",
        target_id=str(row.id),
        detail={"name": row.name, "scopes": chosen, "days": days},
    )
    db.commit()
    db.refresh(row)
    return serialize(row), secret


def _own(db: Session, owner: Principal, token_id: uuid.UUID) -> AccessToken:
    row = db.get(AccessToken, token_id)
    if row is None or row.user_id != owner.user_id:
        raise ApiError(404, "not_found", "Token not found.")
    return row


def revoke(db: Session, owner: Principal, token_id: uuid.UUID) -> dict[str, Any]:
    row = _own(db, owner, token_id)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        audit.record(
            db,
            action="token.revoked",
            actor=owner,
            target_type="access_token",
            target_id=str(row.id),
            detail={"name": row.name},
        )
        db.commit()
        db.refresh(row)
    return serialize(row)


def delete(db: Session, owner: Principal, token_id: uuid.UUID) -> None:
    row = _own(db, owner, token_id)
    audit.record(
        db,
        action="token.deleted",
        actor=owner,
        target_type="access_token",
        target_id=str(row.id),
        detail={"name": row.name},
    )
    db.delete(row)
    db.commit()


def authenticate(db: Session, secret: str) -> Principal | None:
    """The principal a token stands for, or None if it is unknown, expired,
    revoked, or its owner is no longer active."""
    if not secret.startswith(TOKEN_PREFIX) or len(secret) > 200:
        return None
    row = db.scalar(select(AccessToken).where(AccessToken.token_hash == _hash(secret)))
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    if row.last_used_at is None or now - row.last_used_at > TOUCH_EVERY:
        row.last_used_at = now
        db.commit()
    principal = users.principal_for(db, user, "token")
    return dataclasses.replace(principal, scopes=frozenset(row.scopes))
