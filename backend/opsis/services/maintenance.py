"""The nightly clean-up job: rows that have ended and are no use any more.

Ended sessions, used or expired password links and share links that ended long
ago, old sign-in failure counts, and finished jobs older than a month (each
dataset keeps its latest job, which its page shows). The audit log is kept.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from opsis.db.models import Job, LoginAttempt, ShareLink, UserLink
from opsis.db.models import Session as SessionRow
from opsis.settings import Settings

SESSION_GRACE = timedelta(days=7)
LINK_GRACE = timedelta(days=30)
SHARE_LINK_GRACE = timedelta(days=90)
ATTEMPT_AGE = timedelta(days=1)
JOB_AGE = timedelta(days=30)


def clean_up(db: Session, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    idle = timedelta(hours=settings.session_idle_hours)
    counts: dict[str, int] = {}

    def run(name: str, statement: Any) -> None:
        result: Any = db.execute(statement)  # a delete: its row count says how many went
        counts[name] = int(result.rowcount or 0)

    run(
        "sessions",
        delete(SessionRow).where(
            or_(
                SessionRow.revoked_at < now - SESSION_GRACE,
                SessionRow.expires_at < now - SESSION_GRACE,
                SessionRow.last_seen_at < now - idle - SESSION_GRACE,
            )
        ),
    )
    run(
        "passwordLinks",
        delete(UserLink).where(
            or_(UserLink.used_at < now - LINK_GRACE, UserLink.expires_at < now - LINK_GRACE)
        ),
    )
    run(
        "shareLinks",
        delete(ShareLink).where(
            or_(
                ShareLink.revoked_at < now - SHARE_LINK_GRACE,
                ShareLink.expires_at < now - SHARE_LINK_GRACE,
            )
        ),
    )
    run(
        "signInFailures",
        delete(LoginAttempt).where(
            LoginAttempt.first_failure_at < now - ATTEMPT_AGE,
            or_(LoginAttempt.locked_until.is_(None), LoginAttempt.locked_until < now),
        ),
    )
    newest = (
        select(Job.dedupe_key, func.max(Job.created_at).label("latest"))
        .where(Job.dedupe_key.is_not(None))
        .group_by(Job.dedupe_key)
        .subquery()
    )
    keep = select(Job.id).join(
        newest, and_(Job.dedupe_key == newest.c.dedupe_key, Job.created_at == newest.c.latest)
    )
    run(
        "jobs",
        delete(Job).where(
            Job.status.in_(("succeeded", "failed", "cancelled")),
            Job.finished_at < now - JOB_AGE,
            Job.id.not_in(keep),
        ),
    )
    return counts


def run_cleanup(db: Session, job: Job, settings: Settings) -> dict[str, Any]:
    """Job handler. The job runner commits."""
    return {"removed": clean_up(db, settings)}
