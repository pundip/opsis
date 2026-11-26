"""Settings an admin changes in the app, kept in Postgres (ADR-030).

Environment flags still switch whole features on or off at start. These are the
settings that should change at once, without a restart, and leave an audit row.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import WorkspaceSetting
from opsis.services import audit

# May people use API keys of their own in the assistant (v2 V1)? On by default.
BYO_KEYS = "assistant.byo_keys"
DEFAULTS: dict[str, Any] = {BYO_KEYS: True}


def get(db: Session, key: str) -> Any:
    row = db.get(WorkspaceSetting, key)
    return row.value if row is not None else DEFAULTS[key]


def put(db: Session, actor: Principal, key: str, value: Any) -> None:
    """Change a setting, audited. Commits."""
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.get(WorkspaceSetting, key)
    before = row.value if row is not None else DEFAULTS[key]
    if row is None:
        row = WorkspaceSetting(key=key, value=value, updated_by=actor.user_id)
        db.add(row)
    else:
        row.value = value
        row.updated_by = actor.user_id
    audit.record(
        db,
        action="settings.changed",
        actor=actor,
        target_type="setting",
        target_id=key,
        detail={"key": key, "from": before, "to": value},
    )
    db.commit()
