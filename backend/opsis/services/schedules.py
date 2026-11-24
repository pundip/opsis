"""Schedules (FEATURE_SCHEDULED_REFRESH, FEATURE_BACKUPS, FEATURE_EMAIL): cron
expressions in the workspace timezone, run by the job runner's scheduler.

`tick` runs in the one job runner (inline thread or worker container) about
every 30 seconds. Each due row queues its job and moves on to its next time
after now: runs missed while the runner was down are not made up, one is run.
Jobs carry a dedupe key, so a schedule never queues a second refresh of a
dataset that is already refreshing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import Dashboard, Dataset, Schedule
from opsis.errors import ApiError
from opsis.jobs import queue
from opsis.services import audit
from opsis.settings import Settings

log = logging.getLogger("opsis.schedules")

# A schedule may not run more often than this: sources and mailboxes deserve it.
MIN_INTERVAL = timedelta(minutes=15)
CLEANUP_CRON = "15 3 * * *"
BACKUP_JOB = "backup"
CLEANUP_JOB = "cleanup"
SNAPSHOT_JOB = "snapshot.email"

PRESETS = (
    {"id": "hourly", "label": "Every hour", "cron": "0 * * * *"},
    {"id": "every_6_hours", "label": "Every 6 hours", "cron": "0 */6 * * *"},
    {"id": "daily", "label": "Every day at 06:00", "cron": "0 6 * * *"},
    {"id": "weekdays", "label": "Weekdays at 07:00", "cron": "0 7 * * 1-5"},
    {"id": "weekly", "label": "Mondays at 07:00", "cron": "0 7 * * 1"},
    {"id": "monthly", "label": "The 1st of each month at 06:00", "cron": "0 6 1 * *"},
)


def _zone(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.workspace_timezone)


def check_cron(cron: Any, path: str = "/cron") -> str:
    """A valid, not-too-frequent cron expression with five fields, tidied; or 422."""
    text = " ".join(str(cron or "").split())
    problem = None
    if len(text.split()) != 5 or not croniter.is_valid(text):
        problem = (
            "Not a cron expression with five fields (minute hour day month weekday), for "
            "example 0 6 * * * for every day at 06:00."
        )
    else:
        runs = croniter(text, datetime(2026, 1, 1, tzinfo=UTC)).all_next(datetime)
        times = [next(runs) for _ in range(30)]
        if any(b - a < MIN_INTERVAL for a, b in zip(times, times[1:], strict=False)):
            problem = "It runs too often: at most every 15 minutes."
    if problem:
        raise ApiError(422, "invalid_schedule", problem, [{"path": path, "message": problem}])
    return text


def next_after(settings: Settings, cron: str, after: datetime) -> datetime:
    """The next run time after `after`, worked out in the workspace timezone."""
    local = after.astimezone(_zone(settings))
    found: datetime = croniter(cron, local).get_next(datetime)
    return found.astimezone(UTC)


def upcoming(settings: Settings, cron: str, count: int = 3) -> list[str]:
    """The next few run times, as ISO times in the workspace timezone."""
    times, at = [], datetime.now(UTC)
    for _ in range(count):
        at = next_after(settings, cron, at)
        times.append(at.astimezone(_zone(settings)).isoformat())
    return times


def describe(settings: Settings, row: Schedule | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "cron": row.cron,
        "preset": next((p["id"] for p in PRESETS if p["cron"] == row.cron), None),
        "nextRunAt": row.next_run_at.astimezone(_zone(settings)).isoformat(),
        "lastRunAt": row.last_run_at.isoformat() if row.last_run_at else None,
        "timezone": settings.workspace_timezone,
    }


def put_schedule(
    db: Session,
    settings: Settings,
    *,
    key: str,
    kind: str,
    cron: str | None,
    target_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> Schedule | None:
    """Create, change or (with cron None) remove the schedule called `key`."""
    row = db.scalar(select(Schedule).where(Schedule.key == key))
    if cron is None:
        if row is not None:
            db.delete(row)
        return None
    if row is None:
        row = Schedule(
            key=key,
            kind=kind,
            target_id=target_id,
            owner_id=owner_id,
            created_by=created_by,
            cron=cron,
            next_run_at=next_after(settings, cron, datetime.now(UTC)),
        )
        db.add(row)
    elif row.cron != cron:
        row.cron = cron
        row.next_run_at = next_after(settings, cron, datetime.now(UTC))
    return row


# ---- dataset refreshes ---------------------------------------------------------------


def refresh_key(dataset_id: uuid.UUID) -> str:
    return f"refresh:{dataset_id}"


def refresh_schedule(db: Session, settings: Settings, dataset: Dataset) -> dict[str, Any] | None:
    row = db.scalar(select(Schedule).where(Schedule.key == refresh_key(dataset.id)))
    return describe(settings, row)


def set_refresh(
    db: Session, settings: Settings, actor: Principal, dataset: Dataset, cron: str | None
) -> dict[str, Any] | None:
    checked = check_cron(cron) if cron is not None else None
    row = put_schedule(
        db,
        settings,
        key=refresh_key(dataset.id),
        kind="refresh",
        cron=checked,
        target_id=dataset.id,
        created_by=actor.user_id,
    )
    audit.record(
        db,
        action="dataset.schedule_changed",
        actor=actor,
        target_type="dataset",
        target_id=str(dataset.id),
        detail={"dataset": dataset.name, "cron": checked},
    )
    db.commit()
    return describe(settings, row)


# ---- system schedules ------------------------------------------------------------------


def ensure_system(db: Session, settings: Settings) -> None:
    """The backup and clean-up schedules follow the settings. Called at runner start."""
    put_schedule(
        db,
        settings,
        key="backup",
        kind="backup",
        cron=settings.backup_schedule if settings.feature_backups else None,
    )
    put_schedule(db, settings, key="cleanup", kind="cleanup", cron=CLEANUP_CRON)
    db.commit()


def _target_exists(db: Session, row: Schedule) -> bool:
    if row.kind == "refresh":
        return row.target_id is not None and db.get(Dataset, row.target_id) is not None
    if row.kind == "snapshot":
        return row.target_id is not None and db.get(Dashboard, row.target_id) is not None
    return True


def tick(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """Queue the job of every schedule that is due. Returns how many were queued."""
    from opsis.services.datasets import REFRESH_JOB

    now = now or datetime.now(UTC)
    due = db.scalars(
        select(Schedule)
        .where(Schedule.next_run_at <= now)
        .order_by(Schedule.next_run_at)
        .with_for_update(skip_locked=True)
    ).all()
    queued = 0
    for row in due:
        if not _target_exists(db, row):
            db.delete(row)  # its dataset or dashboard was deleted
            continue
        enabled = {
            "refresh": settings.feature_scheduled_refresh,
            "backup": settings.feature_backups,
            "snapshot": settings.feature_email,
            "cleanup": True,
        }.get(row.kind, False)
        if enabled:
            if row.kind == "refresh":
                queue.enqueue(
                    db,
                    REFRESH_JOB,
                    {"dataset_id": str(row.target_id), "scheduled": True},
                    dedupe_key=f"dataset:{row.target_id}",
                )
            elif row.kind == "backup":
                queue.enqueue(db, BACKUP_JOB, {"scheduled": True}, dedupe_key="backup")
            elif row.kind == "cleanup":
                queue.enqueue(db, CLEANUP_JOB, {}, dedupe_key="cleanup")
            elif row.kind == "snapshot":
                queue.enqueue(
                    db, SNAPSHOT_JOB, {"schedule_id": str(row.id)}, dedupe_key=f"snapshot:{row.id}"
                )
            queued += 1
            row.last_run_at = now
        row.next_run_at = next_after(settings, row.cron, now)
    db.commit()
    if queued:
        log.info("Scheduler queued %d job(s)", queued)
    return queued


def forget(db: Session, target_id: uuid.UUID) -> None:
    """Remove the schedules of a dataset or dashboard that is being deleted."""
    db.execute(delete(Schedule).where(Schedule.target_id == target_id))
