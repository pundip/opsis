"""Audit log writes. The caller owns the transaction."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import AuditLog


def record(
    db: Session,
    *,
    action: str,
    actor: Principal | None = None,
    actor_type: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Add an audit row. `detail` must never contain secrets."""
    db.add(
        AuditLog(
            actor_type=actor_type or ("user" if actor else "system"),
            actor_id=actor.user_id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
            ip=ip,
        )
    )
