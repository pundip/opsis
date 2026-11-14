"""Postgres-backed job queue. No Redis: rows are claimed with SKIP LOCKED.

The inline runner (inside api) and the worker container both call claim_next()
and finish(); only the loop around them differs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.db.models import Job

ACTIVE = ("queued", "running")


def enqueue(
    db: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    priority: int = 0,
    created_by: uuid.UUID | None = None,
) -> Job:
    """Add a job. If an active job has the same dedupe_key, return that one instead."""
    if dedupe_key is not None:
        existing = db.scalars(
            select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE))
        ).first()
        if existing is not None:
            return existing
    job = Job(
        type=job_type,
        payload=payload or {},
        dedupe_key=dedupe_key,
        priority=priority,
        created_by=created_by,
        run_after=run_after or datetime.now(UTC),
    )
    db.add(job)
    db.commit()
    return job


def claim_next(db: Session, worker_id: str) -> Job | None:
    """Atomically take the next due job, or None. Safe with many concurrent claimers."""
    job = db.scalars(
        select(Job)
        .where(Job.status == "queued", Job.run_after <= datetime.now(UTC))
        .order_by(Job.priority.desc(), Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.locked_by = worker_id
    job.started_at = datetime.now(UTC)
    job.attempts += 1
    db.commit()
    return job


def fail_stale(db: Session) -> int:
    """Fail jobs left in "running" by a process that stopped. Called at runner start.

    v1 has a single job runner (inline thread or one worker), so any job that
    is "running" when the runner starts cannot still be alive.
    """
    stale = db.scalars(select(Job).where(Job.status == "running").with_for_update()).all()
    for job in stale:
        job.status = "failed"
        job.error = "Interrupted by a restart. Run it again."
        job.finished_at = datetime.now(UTC)
    db.commit()
    return len(stale)


def finish(
    db: Session, job: Job, *, result: dict[str, Any] | None = None, error: str | None = None
) -> None:
    job.status = "failed" if error else "succeeded"
    job.error = error
    job.result = result
    job.finished_at = datetime.now(UTC)
    db.commit()
