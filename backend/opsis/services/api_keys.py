"""People's own API keys for the assistant (v2 V1, ADR-036).

A key is encrypted at rest with ENCRYPTION_KEY (services/crypto.py) and decrypted
only inside the backend, to make a call for its owner. It is never returned to the
browser, logged, or put in an audit row: only its provider, a label and its last
four characters are shown. A person's keys are deleted with them, and when an admin
deactivates them (Opsis has no deleting of people: deactivating ends their access).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import AssistantChoice, AssistantUsage, UserApiKey
from opsis.errors import ApiError
from opsis.services import audit, crypto, workspace_settings
from opsis.settings import Settings

PROVIDERS = {"anthropic": "Anthropic", "openai": "OpenAI"}
MAX_KEYS = 20
# Printable, no spaces: Anthropic keys look like sk-ant-api03-..., OpenAI's sk-proj-...
KEY_SHAPE = re.compile(r"^[\x21-\x7e]{16,512}$")
MODEL_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
UNSET: Any = object()


def describe(row: UserApiKey) -> dict[str, Any]:
    """What the browser may see of a key: never the key itself."""
    return {
        "id": str(row.id),
        "provider": row.provider,
        "providerName": PROVIDERS.get(row.provider, row.provider),
        "label": row.label,
        "last4": row.last4,
        "defaultModel": row.default_model,
        "lastTestOk": row.last_test_ok,
        "lastTestMessage": row.last_test_message,
        "lastTestedAt": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "lastUsedAt": row.last_used_at.isoformat() if row.last_used_at else None,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def owned(db: Session, user_id: uuid.UUID) -> list[UserApiKey]:
    return list(
        db.scalars(
            select(UserApiKey)
            .where(UserApiKey.user_id == user_id)
            .order_by(UserApiKey.created_at, UserApiKey.id)
        ).all()
    )


def own(db: Session, actor: Principal, key_id: uuid.UUID) -> UserApiKey:
    """One of the caller's keys. Someone else's answers 404, as a missing one does."""
    row = db.get(UserApiKey, key_id)
    if row is None or row.user_id != actor.user_id:
        raise ApiError(404, "not_found", "There is no such key.")
    return row


def byo_on(db: Session) -> bool:
    return bool(workspace_settings.get(db, workspace_settings.BYO_KEYS))


def _require_byo(db: Session) -> None:
    if not byo_on(db):
        raise ApiError(
            409,
            "byo_keys_off",
            "An admin has switched off personal API keys in this workspace.",
        )


def _label(text: str) -> str:
    label = " ".join(text.split())[:100]
    if not label:
        raise ApiError(
            422,
            "invalid_request",
            "Give the key a name.",
            [{"path": "/label", "message": "Required."}],
        )
    return label


def clean_model(text: str | None) -> str | None:
    if text is None or not text.strip():
        return None
    model = text.strip()
    if not MODEL_SHAPE.match(model):
        raise ApiError(
            422,
            "invalid_request",
            "That does not look like a model name.",
            [{"path": "/defaultModel", "message": "For example claude-opus-5-5 or gpt-5.5."}],
        )
    return model


def _taken(db: Session, user_id: uuid.UUID, label: str, but: uuid.UUID | None = None) -> None:
    query = select(UserApiKey.id).where(UserApiKey.user_id == user_id, UserApiKey.label == label)
    if but is not None:
        query = query.where(UserApiKey.id != but)
    if db.scalar(query) is not None:
        raise ApiError(
            409,
            "duplicate_label",
            f"You already have a key called {label!r}.",
            [{"path": "/label", "message": "Choose another name."}],
        )


def add(
    db: Session,
    settings: Settings,
    actor: Principal,
    *,
    provider: str,
    label: str,
    key: str,
    default_model: str | None,
) -> UserApiKey:
    _require_byo(db)
    if provider not in PROVIDERS:
        raise ApiError(422, "invalid_request", "The provider must be anthropic or openai.")
    secret = key.strip()
    if not KEY_SHAPE.match(secret):
        raise ApiError(
            422,
            "invalid_request",
            "That does not look like an API key: paste the whole key, without spaces.",
            [{"path": "/key", "message": "Paste the key as the provider shows it."}],
        )
    name = _label(label)
    _taken(db, actor.user_id, name)
    if len(owned(db, actor.user_id)) >= MAX_KEYS:
        raise ApiError(409, "too_many_keys", f"You can keep at most {MAX_KEYS} keys.")
    row = UserApiKey(
        user_id=actor.user_id,
        provider=provider,
        label=name,
        secret=crypto.encrypt(settings, {"key": secret}) or "",
        last4=secret[-4:],
        default_model=clean_model(default_model),
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        action="assistant.key_added",
        actor=actor,
        target_type="api_key",
        target_id=str(row.id),
        detail={"provider": provider, "label": name, "last4": row.last4},
    )
    db.commit()
    return row


def change(
    db: Session,
    actor: Principal,
    key_id: uuid.UUID,
    *,
    label: str | None = None,
    default_model: Any = UNSET,
) -> UserApiKey:
    row = own(db, actor, key_id)
    changed: dict[str, Any] = {}
    if label is not None:
        name = _label(label)
        if name != row.label:
            _taken(db, actor.user_id, name, but=row.id)
            changed["label"] = [row.label, name]
            row.label = name
    if default_model is not UNSET:
        model = clean_model(default_model)
        if model != row.default_model:
            changed["defaultModel"] = [row.default_model, model]
            row.default_model = model
    if changed:
        audit.record(
            db,
            action="assistant.key_changed",
            actor=actor,
            target_type="api_key",
            target_id=str(row.id),
            detail={"provider": row.provider, "label": row.label, **changed},
        )
    db.commit()
    return row


def remove(db: Session, actor: Principal, key_id: uuid.UUID) -> None:
    """Delete one of the caller's keys. If the assistant was set to use it, that choice
    goes too: they deleted it themselves, so the next request uses the default."""
    row = own(db, actor, key_id)
    db.execute(
        delete(AssistantChoice).where(
            AssistantChoice.user_id == actor.user_id, AssistantChoice.option == f"key:{row.id}"
        )
    )
    audit.record(
        db,
        action="assistant.key_deleted",
        actor=actor,
        target_type="api_key",
        target_id=str(row.id),
        detail={"provider": row.provider, "label": row.label, "last4": row.last4},
    )
    db.delete(row)
    db.commit()


def remove_all(db: Session, user_id: uuid.UUID) -> int:
    """Delete every key of a person (deactivated). Does not commit."""
    count = len(owned(db, user_id))
    if count:
        db.execute(delete(UserApiKey).where(UserApiKey.user_id == user_id))
    return count


def secret(settings: Settings, row: UserApiKey) -> str:
    """The key itself, for a call on its owner's behalf. Never return or log it."""
    value = crypto.decrypt(settings, row.secret).get("key")
    if not isinstance(value, str) or not value:
        raise ApiError(
            409, "key_unreadable", "This key cannot be read. Delete it and add it again."
        )
    return value


def record_test(db: Session, row: UserApiKey, ok: bool, message: str) -> None:
    row.last_test_ok = ok
    row.last_test_message = message[:1000]
    row.last_tested_at = datetime.now(UTC)
    db.commit()


def providers_by_person(db: Session) -> dict[str, dict[str, int]]:
    """For admins: how many keys of each provider each person has. Never the keys."""
    counts: dict[str, dict[str, int]] = {}
    for user_id, provider, count in db.execute(
        select(UserApiKey.user_id, UserApiKey.provider, func.count()).group_by(
            UserApiKey.user_id, UserApiKey.provider
        )
    ).all():
        counts.setdefault(str(user_id), {})[provider] = int(count)
    return counts


def usage(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> list[dict[str, Any]]:
    """The person's assistant use, per key (or the workspace's options) and provider:
    requests and tokens in all, and in the last 30 days."""
    since = (now or datetime.now(UTC)) - timedelta(days=30)
    recent = AssistantUsage.created_at >= since
    rows = db.execute(
        select(
            AssistantUsage.source,
            AssistantUsage.provider,
            AssistantUsage.key_id,
            AssistantUsage.key_label,
            func.count(),
            func.sum(AssistantUsage.input_tokens),
            func.sum(AssistantUsage.output_tokens),
            func.count().filter(recent),
            func.coalesce(func.sum(AssistantUsage.input_tokens).filter(recent), 0),
            func.coalesce(func.sum(AssistantUsage.output_tokens).filter(recent), 0),
            func.max(AssistantUsage.created_at),
        )
        .where(AssistantUsage.user_id == user_id)
        .group_by(
            AssistantUsage.source,
            AssistantUsage.provider,
            AssistantUsage.key_id,
            AssistantUsage.key_label,
        )
        .order_by(func.max(AssistantUsage.created_at).desc())
    ).all()
    return [
        {
            "source": source,
            "provider": provider,
            "keyId": str(key_id) if key_id else None,
            "keyLabel": label,
            "requests": int(requests),
            "inputTokens": int(input_tokens or 0),
            "outputTokens": int(output_tokens or 0),
            "last30Days": {
                "requests": int(recent_requests),
                "inputTokens": int(recent_input),
                "outputTokens": int(recent_output),
            },
            "lastAt": last.isoformat() if last else None,
        }
        for (
            source,
            provider,
            key_id,
            label,
            requests,
            input_tokens,
            output_tokens,
            recent_requests,
            recent_input,
            recent_output,
            last,
        ) in rows  # fmt: skip
    ]
