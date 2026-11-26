"""Capabilities an admin gives to, or takes from, people and groups (ADR-030).

One rule for every capability: a setting on the person wins; otherwise a "no" on
any of their groups wins over a "yes"; otherwise the capability's default. In v2
V1 there is one: "may use the workspace key" (off by default). V7 adds "may export".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import CapabilityGrant, GroupMember
from opsis.services import audit

WORKSPACE_KEY = "assistant.workspace_key"
DEFAULTS: dict[str, bool] = {WORKSPACE_KEY: False}


def group_ids(db: Session, user_id: uuid.UUID) -> list[str]:
    """The person's groups as they are now (not as their session began)."""
    rows = db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id)).all()
    return [str(group) for group in rows]


def allowed(
    db: Session, capability: str, user_id: uuid.UUID, groups: Iterable[str] | None = None
) -> bool:
    groups = list(groups) if groups is not None else group_ids(db, user_id)
    names = [f"user:{user_id}", *(f"group:{group}" for group in groups)]
    rows = dict(
        db.execute(
            select(CapabilityGrant.grantee, CapabilityGrant.allowed).where(
                CapabilityGrant.capability == capability, CapabilityGrant.grantee.in_(names)
            )
        ).all()
    )
    own = rows.get(f"user:{user_id}")
    if own is not None:
        return bool(own)
    by_group = [value for name, value in rows.items() if name.startswith("group:")]
    if any(value is False for value in by_group):
        return False
    if by_group:
        return True
    return DEFAULTS[capability]


def rows(db: Session, capability: str) -> dict[str, bool]:
    """Every setting of a capability, by grantee."""
    return dict(
        db.execute(
            select(CapabilityGrant.grantee, CapabilityGrant.allowed).where(
                CapabilityGrant.capability == capability
            )
        ).all()
    )


def set_grant(
    db: Session, actor: Principal, capability: str, grantee: str, value: bool | None
) -> None:
    """Allow (True), withhold (False) or go back to the default (None). Audited; commits."""
    if capability not in DEFAULTS:
        raise KeyError(capability)
    row = db.scalar(
        select(CapabilityGrant).where(
            CapabilityGrant.capability == capability, CapabilityGrant.grantee == grantee
        )
    )
    before = row.allowed if row is not None else None
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(
            CapabilityGrant(
                capability=capability, grantee=grantee, allowed=value, created_by=actor.user_id
            )
        )
    else:
        row.allowed = value
    audit.record(
        db,
        action="grant.changed",
        actor=actor,
        target_type="capability",
        target_id=capability,
        detail={"capability": capability, "grantee": grantee, "from": before, "to": value},
    )
    db.commit()


def forget(db: Session, grantee: str) -> None:
    """Remove a deleted group's (or person's) settings. Does not commit."""
    db.execute(delete(CapabilityGrant).where(CapabilityGrant.grantee == grantee))
