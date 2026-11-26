"""Assistant conversations: one person's chat about one dashboard, kept in Postgres.

Two lists are kept per conversation. `assistant_messages` is the history in the
provider's own format, appended to and sent back unchanged. `assistant_entries`
is what the panel shows: requests, replies, tool steps and notices.

Only the person who had a conversation can read it, admins included: it is
their working notes, and it can contain data from datasets others may not use.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opsis.db.models import AssistantConversation, AssistantEntry, AssistantMessage

# The panel shows at most this many entries of a conversation.
MAX_ENTRIES = 500


def latest(
    db: Session, dashboard_id: uuid.UUID, user_id: uuid.UUID
) -> AssistantConversation | None:
    return db.scalar(
        select(AssistantConversation)
        .where(
            AssistantConversation.dashboard_id == dashboard_id,
            AssistantConversation.user_id == user_id,
        )
        .order_by(AssistantConversation.created_at.desc(), AssistantConversation.id)
        .limit(1)
    )


def start(
    db: Session,
    dashboard_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    model: str,
    option: str | None = None,
) -> AssistantConversation:
    conversation = AssistantConversation(
        dashboard_id=dashboard_id, user_id=user_id, provider=provider, model=model, option=option
    )
    db.add(conversation)
    db.commit()
    return conversation


def history(db: Session, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    return list(
        db.scalars(
            select(AssistantMessage.message)
            .where(AssistantMessage.conversation_id == conversation_id)
            .order_by(AssistantMessage.id)
        ).all()
    )


def append_messages(
    db: Session, conversation_id: uuid.UUID, messages: list[dict[str, Any]]
) -> None:
    for message in messages:
        db.add(AssistantMessage(conversation_id=conversation_id, message=message))
    db.commit()


def _entry(row: AssistantEntry) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "data": row.data,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


def add_entry(
    db: Session, conversation_id: uuid.UUID, kind: str, data: dict[str, Any]
) -> dict[str, Any]:
    row = AssistantEntry(conversation_id=conversation_id, kind=kind, data=data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _entry(row)


def entries(db: Session, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(AssistantEntry)
        .where(AssistantEntry.conversation_id == conversation_id)
        .order_by(AssistantEntry.id.desc())
        .limit(MAX_ENTRIES)
    ).all()
    return [_entry(row) for row in reversed(rows)]


def add_usage(
    db: Session, conversation_id: uuid.UUID, input_tokens: int, output_tokens: int
) -> None:
    db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(
            input_tokens=AssistantConversation.input_tokens + input_tokens,
            output_tokens=AssistantConversation.output_tokens + output_tokens,
        )
    )
    db.commit()


def describe(conversation: AssistantConversation) -> dict[str, Any]:
    return {
        "id": str(conversation.id),
        "provider": conversation.provider,
        "model": conversation.model,
        "option": conversation.option,
        "inputTokens": conversation.input_tokens,
        "outputTokens": conversation.output_tokens,
        "createdAt": conversation.created_at.isoformat() if conversation.created_at else None,
    }
