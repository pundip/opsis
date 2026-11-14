"""Permissions, migrations, the job queue and the built-in admin."""

from __future__ import annotations

import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from opsis.auth.principal import ROLE_PERMISSIONS, Permission, Principal, Role
from opsis.db.models import AuditLog, Base, User
from opsis.jobs import queue, worker
from opsis.services import users
from opsis.settings import Settings


def principal(role: Role, allowed: frozenset[Permission] | None = None) -> Principal:
    return Principal(uuid.uuid4(), "a@b.c", "A", role, "session", allowed=allowed)


def test_role_permissions() -> None:
    viewer, editor, admin = (principal(r) for r in (Role.VIEWER, Role.EDITOR, Role.ADMIN))
    assert viewer.can(Permission.DASHBOARDS_VIEW)
    assert not viewer.can(Permission.DASHBOARDS_EDIT)
    assert editor.can(Permission.DASHBOARDS_EDIT) and editor.can(Permission.MODELS_EDIT)
    assert not editor.can(Permission.CONNECTIONS_MANAGE)
    assert not editor.can(Permission.SQL_LAB_USE)
    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)
    assert all(admin.can(p) for p in Permission)


def test_token_scopes_can_only_narrow_a_role() -> None:
    narrowed = principal(Role.ADMIN, frozenset({Permission.DASHBOARDS_VIEW}))
    assert narrowed.permissions == {Permission.DASHBOARDS_VIEW}
    widened = principal(Role.VIEWER, frozenset({Permission.USERS_MANAGE}))
    assert not widened.can(Permission.USERS_MANAGE)


def test_migrations_match_models(migrated_db: Settings) -> None:
    engine = create_engine(migrated_db.sqlalchemy_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_builtin_admin_is_created_once_and_audited(db: Session) -> None:
    first = users.ensure_builtin_admin(db)
    second = users.ensure_builtin_admin(db)
    assert first.id == second.id and first.password_hash is None
    assert db.scalar(select(func.count()).select_from(User)) == 1
    assert db.scalar(select(AuditLog.action)) == "user.builtin_admin_created"


def test_queue_claims_each_job_once_in_priority_order(db: Session) -> None:
    low = queue.enqueue(db, "noop", {"n": 1})
    high = queue.enqueue(db, "noop", {"n": 2}, priority=5)
    first = queue.claim_next(db, "w1")
    second = queue.claim_next(db, "w2")
    assert first is not None and second is not None
    assert (first.id, second.id) == (high.id, low.id)
    assert first.status == "running" and first.locked_by == "w1" and first.attempts == 1
    assert queue.claim_next(db, "w3") is None


def test_queue_dedupes_active_jobs(db: Session) -> None:
    a = queue.enqueue(db, "refresh", dedupe_key="dataset:1")
    b = queue.enqueue(db, "refresh", dedupe_key="dataset:1")
    assert a.id == b.id
    claimed = queue.claim_next(db, "w1")
    assert claimed is not None
    queue.finish(db, claimed, result={"rows": 3})
    c = queue.enqueue(db, "refresh", dedupe_key="dataset:1")
    assert c.id != a.id and claimed.status == "succeeded"


def test_worker_runs_handlers_and_records_failures(db: Session) -> None:
    def boom(_: Session, __: object) -> None:
        raise RuntimeError("source is down")

    worker.HANDLERS.update(ok=lambda _db, _job: {"done": True}, boom=boom)
    try:
        jobs = [queue.enqueue(db, t) for t in ("ok", "boom", "unknown")]
        while worker.run_one(db, "w1"):
            pass
    finally:
        worker.HANDLERS.clear()
    for job in jobs:
        db.refresh(job)
    assert [j.status for j in jobs] == ["succeeded", "failed", "failed"]
    assert jobs[0].result == {"done": True}
    assert jobs[1].error == "RuntimeError: source is down"
    assert "No handler" in (jobs[2].error or "")
    assert worker.run_one(db, "w1") is False
