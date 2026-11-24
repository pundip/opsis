"""Admin routes: users and groups (sign-in modes only) and the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.auth.principal import Permission, Principal
from opsis.db.models import AuditLog, User
from opsis.services import access, accounts, audit, mail
from opsis.settings import Settings

users_router = APIRouter(prefix="/api")
audit_router = APIRouter(prefix="/api")

manage = require(Permission.USERS_MANAGE)
audit_view = require(Permission.AUDIT_VIEW)

Role = Literal["admin", "editor", "viewer"]


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    displayName: str = Field(default="", max_length=200)
    role: Role = "viewer"
    attributes: dict[str, Any] = Field(default_factory=dict)
    groupIds: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class UserPatch(BaseModel):
    displayName: str | None = Field(default=None, max_length=200)
    role: Role | None = None
    attributes: dict[str, Any] | None = None
    groupIds: list[uuid.UUID] | None = Field(default=None, max_length=200)
    isActive: bool | None = None


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    memberIds: list[uuid.UUID] | None = Field(default=None, max_length=2000)


@users_router.get("/users")
def list_users(
    db: Session = Depends(get_db), _: Principal = Depends(manage)
) -> list[dict[str, Any]]:
    return accounts.list_users(db)


@users_router.post("/users", status_code=201)
def invite_user(
    body: InviteIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    """With FEATURE_EMAIL the invitation is emailed. Otherwise, or if sending
    fails, the link is returned once to pass on to the person."""
    user, url = accounts.invite(
        db,
        settings,
        actor,
        email=body.email,
        display_name=body.displayName,
        role=body.role,
        attributes=body.attributes,
        group_ids=body.groupIds,
    )
    message = mail.invitation(settings, user["displayName"], url, actor.display_name)
    return {"user": user, **_deliver(db, settings, actor, user["email"], url, message)}


def _deliver(
    db: Session, settings: Settings, actor: Principal, to: str, url: str, message: mail.Message
) -> dict[str, Any]:
    """Email a password link, or hand it back: {link, emailed, emailError}."""
    if not settings.feature_email:
        return {"link": url, "emailed": False}
    try:
        mail.send(settings, to, message)
    except mail.MailError as exc:
        return {"link": url, "emailed": False, "emailError": exc.message}
    audit.record(
        db, action="user.link_emailed", actor=actor, target_type="user", detail={"email": to}
    )
    db.commit()
    return {"link": None, "emailed": True}


@users_router.patch("/users/{user_id}")
def update_user(
    user_id: uuid.UUID,
    body: UserPatch,
    db: Session = Depends(get_db),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    return accounts.update_user(db, actor, user_id, body.model_dump(exclude_unset=True))


@users_router.post("/users/{user_id}/password-link")
def password_link(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    """A new invitation (never signed in) or reset link: emailed with
    FEATURE_EMAIL, otherwise returned once."""
    made = accounts.password_link(db, settings, actor, user_id)
    message = (
        mail.invitation(settings, made["name"], made["url"], actor.display_name)
        if made["purpose"] == "invite"
        else mail.password_reset(settings, made["name"], made["url"])
    )
    return _deliver(db, settings, actor, made["email"], made["url"], message)


@users_router.get("/groups")
def list_groups(
    db: Session = Depends(get_db), _: Principal = Depends(manage)
) -> list[dict[str, Any]]:
    return access.list_groups(db)


@users_router.post("/groups", status_code=201)
def create_group(
    body: GroupIn, db: Session = Depends(get_db), actor: Principal = Depends(manage)
) -> dict[str, Any]:
    return access.save_group(
        db, actor, None, name=body.name, description=body.description, member_ids=body.memberIds
    )


@users_router.put("/groups/{group_id}")
def update_group(
    group_id: uuid.UUID,
    body: GroupIn,
    db: Session = Depends(get_db),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    return access.save_group(
        db, actor, group_id, name=body.name, description=body.description, member_ids=body.memberIds
    )


@users_router.delete("/groups/{group_id}", status_code=204)
def delete_group(
    group_id: uuid.UUID, db: Session = Depends(get_db), actor: Principal = Depends(manage)
) -> None:
    access.delete_group(db, actor, group_id)


# ---- audit log ----------------------------------------------------------------------

AUDIT_PAGE = 100


@audit_router.get("/audit")
def list_audit(
    action: str | None = None,
    actor: str | None = None,
    before: int | None = None,
    limit: int = AUDIT_PAGE,
    db: Session = Depends(get_db),
    _: Principal = Depends(audit_view),
) -> dict[str, Any]:
    """Newest first. `action` matches a prefix ("auth." or "agent."), `actor` part
    of a name or email, `before` is the id to page from."""
    query = (
        select(AuditLog, User.display_name, User.email)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .order_by(AuditLog.id.desc())
        .limit(max(1, min(limit, 500)))
    )
    if action:
        query = query.where(AuditLog.action.startswith(action.strip()[:100]))
    if actor:
        pattern = f"%{actor.strip()[:100].replace('%', '').replace('_', '')}%"
        query = query.where(or_(User.display_name.ilike(pattern), User.email.ilike(pattern)))
    if before is not None:
        query = query.where(AuditLog.id < before)
    rows = db.execute(query).all()
    entries = [
        {
            "id": row.id,
            "ts": row.ts.isoformat() if isinstance(row.ts, datetime) else None,
            "actorType": row.actor_type,
            "actorName": name,
            "actorEmail": email,
            "action": row.action,
            "targetType": row.target_type,
            "targetId": row.target_id,
            "detail": row.detail,
            "ip": row.ip,
        }
        for row, name, email in rows
    ]
    return {
        "entries": entries,
        "next": entries[-1]["id"] if len(entries) == max(1, min(limit, 500)) else None,
    }
