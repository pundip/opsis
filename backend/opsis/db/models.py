"""SQLAlchemy models. Postgres holds metadata only, never analytical data.

M1 baseline: users, sessions, audit_log, jobs. Any change here needs a matching
Alembic migration; tests/test_migrations.py fails when they drift apart.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMPTY_JSON = text("'{}'::jsonb")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower case
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # admin | editor | viewer
    password_hash: Mapped[str | None] = mapped_column(String(255))
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    is_builtin: Mapped[bool] = mapped_column(default=False, server_default=false())
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Free-form attributes referenced by row-level security rules.
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=EMPTY_JSON
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    """Server-side browser session. The id is the SHA-256 of the cookie value."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(20))  # user | agent | system
    # No foreign key: audit rows must survive user deletion.
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=EMPTY_JSON)
    ip: Mapped[str | None] = mapped_column(String(45))


class Connection(Base):
    """A configured source. Secret fields are stored encrypted, apart from the rest."""

    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    connector_id: Mapped[str] = mapped_column(String(50))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=EMPTY_JSON)
    secrets: Mapped[str | None] = mapped_column(Text)  # Fernet token, never returned by the API
    last_test_ok: Mapped[bool | None] = mapped_column()
    last_test_message: Mapped[str | None] = mapped_column(Text)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Dataset(Base):
    """One stream of a connection, pulled into local Parquet snapshots."""

    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Identifier used in semantic queries ("gifts"), see spec/semantic-query.schema.json.
    name: Mapped[str] = mapped_column(String(63), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT"), index=True
    )
    stream: Mapped[str] = mapped_column(String(300))
    refresh_mode: Mapped[str] = mapped_column(String(20))  # full | append | merge
    cursor_field: Mapped[str | None] = mapped_column(String(200))
    primary_key: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    columns: Mapped[list[str] | None] = mapped_column(JSONB)  # None means all columns
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # connector cursor state
    # Points at the snapshot readers use. Updated in one transaction with `state`.
    current_snapshot_id: Mapped[uuid.UUID | None] = mapped_column()
    # When true, only admins and the grantees in dataset_grants may use the dataset.
    restricted: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Who set the restriction. When an admin did, only an admin may lift it.
    restricted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    restricted_by_admin: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Snapshot(Base):
    """An immutable Parquet directory on the data volume."""

    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    row_count: Mapped[int] = mapped_column(BigInteger)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    columns: Mapped[list[dict[str, str]]] = mapped_column(JSONB)  # [{name, type}]
    mode: Mapped[str] = mapped_column(String(20))  # how this snapshot was built
    job_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SemanticModel(Base):
    """The semantic model of one dataset (spec/semantic-model.schema.json)."""

    __tablename__ = "semantic_models"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), unique=True
    )
    # JSON, not JSONB: JSONB re-sorts keys, and a model must keep the order its
    # author wrote (fields are listed and exported to YAML in that order).
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Number of the newest row in semantic_model_versions for this dataset.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # True until someone edits it: a generated starter can be regenerated safely.
    is_starter: Mapped[bool] = mapped_column(default=True, server_default=true())
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SemanticModelVersion(Base):
    """An immutable copy of a model, written on every save.

    The same pattern as dashboard versions: rows are never updated, the author
    is a user, the agent or the system, and restoring makes a new version.
    """

    __tablename__ = "semantic_model_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    author_type: Mapped[str] = mapped_column(String(20))  # user | agent | system
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dashboard(Base):
    """A dashboard. Its content lives in dashboard_versions.

    Draft is always the newest version (`version`). Published is a pointer to
    one specific version; viewers and share links only ever read that one.
    """

    __tablename__ = "dashboards"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))  # copied from the newest spec, for lists
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    published_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DashboardVersion(Base):
    """An immutable dashboard spec (spec/dashboard.schema.json), one row per save."""

    __tablename__ = "dashboard_versions"
    __table_args__ = (UniqueConstraint("dashboard_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)  # JSON keeps the author's key order
    author_type: Mapped[str] = mapped_column(String(20))  # user | agent | system
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    """Job queue row, claimed with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after"),
        # At most one queued or running job per dedupe key (one refresh per dataset).
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=EMPTY_JSON)
    # queued | running | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_by: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessToken(Base):
    """A personal access token for the MCP server. Only a hash is stored;
    the token itself is shown once, when it is created."""

    __tablename__ = "access_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    # SHA-256 of the token, hex. The token is 256 random bits, so a fast hash is enough.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The first characters of the token, to recognise it in a list.
    prefix: Mapped[str] = mapped_column(String(20))
    scopes: Mapped[list[str]] = mapped_column(JSONB)  # read | edit | publish | admin
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_access_tokens_user_id", "user_id"),)


# ---- M6: sign-in, groups, access and sharing ---------------------------------------
#
# A "grantee" is written "user:<uuid>", "group:<uuid>" or "everyone" (every active
# user), so one column covers people, groups and the whole workspace.


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DashboardShare(Base):
    """Who may view or edit a dashboard, besides its owner and the admins."""

    __tablename__ = "dashboard_shares"
    __table_args__ = (UniqueConstraint("dashboard_id", "grantee"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"), index=True
    )
    grantee: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(10))  # viewer | editor
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DatasetGrant(Base):
    """Who may use a restricted dataset (datasets.restricted), besides the admins."""

    __tablename__ = "dataset_grants"
    __table_args__ = (UniqueConstraint("dataset_id", "grantee"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    grantee: Mapped[str] = mapped_column(String(60))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShareLink(Base):
    """A public, read-only link to the published version of a dashboard.
    Only the SHA-256 of the token is stored; the link is shown once."""

    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(100))
    # The user attributes row-level security sees for everyone who opens the link.
    # Empty means every rule denies.
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=EMPTY_JSON
    )
    password_hash: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    view_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginAttempt(Base):
    """Failed sign-in attempts per key ("email:...", "ip:...", "link:..."), for lockouts."""

    __tablename__ = "login_attempts"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    first_failure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserLink(Base):
    """A one-time link to set a password: an invitation or a reset by an admin."""

    __tablename__ = "user_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(10))  # invite | reset
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssistantConversation(Base):
    """One person's chat with the in-app assistant about one dashboard."""

    __tablename__ = "assistant_conversations"
    __table_args__ = (Index("ix_assistant_conversations_lookup", "dashboard_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dashboards.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # The history can only be sent back to the provider and model that wrote it.
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AssistantMessage(Base):
    """A message in the provider's own format, stored exactly as it is sent back.

    JSON rather than JSONB keeps the key order, so the history the provider sees
    is the same bytes every time (prompt caching and Claude's thinking blocks
    depend on that). Rows are only ever appended.
    """

    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    message: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssistantEntry(Base):
    """What the assistant panel shows: requests, replies, tool steps and notices."""

    __tablename__ = "assistant_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))  # request | text | update | step | notice | done
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Schedule(Base):
    """Something to do on a cron schedule, run by the job runner's scheduler.

    `key` names what it is for, so there is one row per thing: "refresh:<dataset
    id>", "snapshot:<dashboard id>:<user id>" (an emailed dashboard), "backup" and
    "cleanup". Times are cron expressions in the workspace timezone.
    """

    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(30))  # refresh | snapshot | backup | cleanup
    target_id: Mapped[uuid.UUID | None] = mapped_column()  # the dataset or dashboard
    # For a snapshot: the person it is emailed to, and whose access it is built with.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    cron: Mapped[str] = mapped_column(String(100))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
