"""Routes for people's own API keys (Account page) and the admin's controls for the
assistant (Users page). v2 V1, ADR-036.

Keys go in once and never come back out: every answer carries the provider, the
label and the last four characters, never the key. The Test button and the model
list ask the provider's models endpoint, which costs nothing.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.api.assistant import person
from opsis.api.deps import get_db, get_settings, require
from opsis.assistant import choices, providers
from opsis.auth.principal import Permission, Principal
from opsis.db.models import Group, GroupMember, User
from opsis.errors import ApiError
from opsis.services import api_keys, grants, workspace_settings
from opsis.settings import Settings

router = APIRouter(prefix="/api")
access_router = APIRouter(prefix="/api")

manage = require(Permission.USERS_MANAGE)


class KeyIn(BaseModel):
    provider: Literal["anthropic", "openai"]
    label: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=1, max_length=600)
    defaultModel: str | None = Field(default=None, max_length=200)


class KeyPatch(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    defaultModel: str | None = Field(default=None, max_length=200)


class AccessSettingsIn(BaseModel):
    byoKeys: bool


class GrantIn(BaseModel):
    grantee: str = Field(min_length=6, max_length=60)
    # True allows, False withholds (wins over a group's yes), None goes back to the default.
    allowed: bool | None


def _transport(request: Request) -> Any:
    return request.app.state.assistant_transport


@router.get("/account/api-keys")
def list_keys(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(person),
) -> dict[str, Any]:
    return {
        "enabled": api_keys.byo_on(db),
        "keys": [api_keys.describe(row) for row in api_keys.owned(db, principal.user_id)],
        "workspaceKey": choices.may_use_workspace_key(db, settings, principal.user_id),
        "ollama": settings.ollama_on,
        "usage": api_keys.usage(db, principal.user_id),
    }


@router.post("/account/api-keys", status_code=201)
def add_key(
    body: KeyIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(person),
) -> dict[str, Any]:
    row = api_keys.add(
        db,
        settings,
        principal,
        provider=body.provider,
        label=body.label,
        key=body.key,
        default_model=body.defaultModel,
    )
    return api_keys.describe(row)


@router.patch("/account/api-keys/{key_id}")
def change_key(
    key_id: uuid.UUID,
    body: KeyPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(person),
) -> dict[str, Any]:
    sent = body.model_dump(exclude_unset=True)
    row = api_keys.change(
        db,
        principal,
        key_id,
        label=sent.get("label"),
        default_model=sent.get("defaultModel", api_keys.UNSET),
    )
    return api_keys.describe(row)


@router.delete("/account/api-keys/{key_id}", status_code=204)
def delete_key(
    key_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(person)
) -> Response:
    api_keys.remove(db, principal, key_id)
    return Response(status_code=204)


@router.post("/account/api-keys/{key_id}/test")
def test_key(
    key_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(person),
) -> dict[str, Any]:
    """Ask the provider which models the key may use: free, and proof that it works."""
    row = api_keys.own(db, principal, key_id)
    secret = api_keys.secret(settings, row)
    db.rollback()  # hold nothing open during the call
    result = providers.check_key(
        settings, row.provider, secret, row.default_model, _transport(request)
    )
    row = api_keys.own(db, principal, key_id)
    api_keys.record_test(db, row, result.ok, result.message)
    return {
        "ok": result.ok,
        "message": result.message,
        "models": result.models,
        "key": api_keys.describe(row),
    }


@router.get("/account/api-keys/{key_id}/models")
def key_models(
    key_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(person),
) -> dict[str, Any]:
    row = api_keys.own(db, principal, key_id)
    secret = api_keys.secret(settings, row)
    provider = row.provider
    db.rollback()
    try:
        models = providers.list_models(settings, provider, secret, _transport(request))
    except providers.ProviderError as error:
        raise ApiError(502, "provider_error", error.message) from None
    return {"models": models}


# ---- admins: who may use the workspace key, and personal keys on or off ------------


def _effective(user_id: str, memberships: dict[str, list[str]], rows: dict[str, bool]) -> bool:
    """grants.allowed, worked out from rows already read (one person of many)."""
    own = rows.get(f"user:{user_id}")
    if own is not None:
        return own
    by_group = [rows[f"group:{g}"] for g in memberships.get(user_id, []) if f"group:{g}" in rows]
    if any(value is False for value in by_group):
        return False
    return bool(by_group) or grants.DEFAULTS[grants.WORKSPACE_KEY]


@access_router.get("/assistant-access")
def get_access(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(manage),
) -> dict[str, Any]:
    rows = grants.rows(db, grants.WORKSPACE_KEY)
    memberships: dict[str, list[str]] = {}
    for user_id, group_id in db.execute(select(GroupMember.user_id, GroupMember.group_id)).all():
        memberships.setdefault(str(user_id), []).append(str(group_id))
    keys = api_keys.providers_by_person(db)
    people = [
        {
            "id": str(user.id),
            "displayName": user.display_name,
            "email": user.email,
            "role": user.role,
            "isActive": user.is_active,
            "grant": rows.get(f"user:{user.id}"),
            "workspaceKey": _effective(str(user.id), memberships, rows),
            "keys": keys.get(str(user.id), {}),
        }
        for user in db.scalars(
            select(User).where(User.is_builtin.is_(False)).order_by(User.display_name, User.email)
        )
    ]
    groups = [
        {"id": str(group.id), "name": group.name, "grant": rows.get(f"group:{group.id}")}
        for group in db.scalars(select(Group).order_by(Group.name))
    ]
    return {
        "byoKeys": api_keys.byo_on(db),
        "workspaceKey": {
            "configured": settings.workspace_key_on,
            "model": settings.anthropic_model.strip(),
        },
        "ollama": {"enabled": settings.ollama_on, "model": settings.ollama_model.strip()},
        "people": people,
        "groups": groups,
    }


@access_router.put("/assistant-access/settings")
def put_access_settings(
    body: AccessSettingsIn, db: Session = Depends(get_db), actor: Principal = Depends(manage)
) -> dict[str, Any]:
    workspace_settings.put(db, actor, workspace_settings.BYO_KEYS, body.byoKeys)
    return {"byoKeys": api_keys.byo_on(db)}


@access_router.put("/assistant-access/grants")
def put_grant(
    body: GrantIn, db: Session = Depends(get_db), actor: Principal = Depends(manage)
) -> dict[str, Any]:
    kind, _, raw = body.grantee.partition(":")
    try:
        target = uuid.UUID(raw)
    except ValueError:
        target = None
    found = (
        db.get(User, target)
        if kind == "user" and target
        else db.get(Group, target)
        if kind == "group" and target
        else None
    )
    if found is None:
        raise ApiError(
            404,
            "not_found",
            "Name a person (user:<id>) or a group (group:<id>) that exists.",
            [{"path": "/grantee", "message": "No such person or group."}],
        )
    grantee = f"{kind}:{target}"
    grants.set_grant(db, actor, grants.WORKSPACE_KEY, grantee, body.allowed)
    return {"grantee": grantee, "allowed": body.allowed}
