"""REST routes for schedules (FEATURE_SCHEDULED_REFRESH), backups (FEATURE_BACKUPS),
the test email and emailed dashboards (FEATURE_EMAIL).

Each router is registered only when its flag is on, so with the flag off its
routes answer 404.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.api.errors import ApiError
from opsis.auth.principal import Permission, Principal
from opsis.db.models import Dashboard, Dataset, Schedule, User
from opsis.jobs import queue
from opsis.services import access, audit, backups, dashboards, datasets, demo, mail, schedules
from opsis.services import subscriptions as subs
from opsis.settings import Settings

edit = require(Permission.DATASETS_EDIT)
edit_read = require(Permission.DATASETS_EDIT, read_only=True)
admin = require(Permission.SETTINGS_MANAGE)
view = require(Permission.DASHBOARDS_VIEW)

schedules_router = APIRouter(prefix="/api")
backups_router = APIRouter(prefix="/api/admin")


class CronIn(BaseModel):
    cron: str = Field(max_length=100)


class ScheduleIn(BaseModel):
    # None removes the schedule.
    cron: str | None = Field(default=None, max_length=100)


@schedules_router.get("/schedules/presets")
def presets(
    settings: Settings = Depends(get_settings), principal: Principal = Depends(edit)
) -> dict[str, Any]:
    return {"presets": list(schedules.PRESETS), "timezone": settings.workspace_timezone}


@schedules_router.post("/schedules/preview")
def preview(
    body: CronIn,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(edit_read),
) -> dict[str, Any]:
    cron = schedules.check_cron(body.cron)
    return {
        "cron": cron,
        "next": schedules.upcoming(settings, cron),
        "timezone": settings.workspace_timezone,
    }


@schedules_router.get("/schedules")
def list_schedules(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(edit),
) -> dict[str, Any]:
    """Refresh schedules of the datasets the caller may use, and the caller's
    emailed dashboards; admins also see everyone's, the backup and the clean-up."""
    blocked = access.blocked_datasets(db, principal)
    names = {d.id: d for d in db.scalars(select(Dataset)).all()}
    titles = {id_: title for id_, title in db.execute(select(Dashboard.id, Dashboard.title))}
    people = {id_: name for id_, name in db.execute(select(User.id, User.display_name))}
    rows = []
    for row in db.scalars(select(Schedule).order_by(Schedule.next_run_at)).all():
        entry = {"id": str(row.id), "kind": row.kind, **(schedules.describe(settings, row) or {})}
        if row.kind == "refresh":
            dataset = names.get(row.target_id) if row.target_id else None
            if dataset is None or dataset.name in blocked:
                continue
            entry |= {
                "datasetId": str(dataset.id),
                "dataset": dataset.name,
                "label": dataset.label,
                "paused": schedules.paused(db, dataset.id),
            }
        elif row.kind in ("backup", "cleanup"):
            if not principal.is_admin:
                continue
            entry["label"] = (
                "Backup" if row.kind == "backup" else "Clean-up of ended sessions and links"
            )
        elif row.kind == "snapshot":
            if row.owner_id != principal.user_id and not principal.is_admin:
                continue
            title = titles.get(row.target_id) if row.target_id else None
            if title is None:
                continue
            entry |= {
                "dashboardId": str(row.target_id),
                "label": f"Email of {title} to {people.get(row.owner_id, 'someone')}"
                if row.owner_id
                else title,
            }
        else:
            continue
        rows.append(entry)
    return {"schedules": rows, "timezone": settings.workspace_timezone}


@schedules_router.put("/datasets/{dataset_id}/schedule")
def set_schedule(
    dataset_id: uuid.UUID,
    body: ScheduleIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
) -> dict[str, Any]:
    dataset = datasets.get(db, dataset_id)
    access.require_datasets(db, actor, {dataset.name})
    return {"schedule": schedules.set_refresh(db, settings, actor, dataset, body.cron)}


@backups_router.get("/backups")
def list_backups(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(admin),
) -> dict[str, Any]:
    row = db.scalar(select(Schedule).where(Schedule.key == "backup"))
    return {
        "backups": backups.backups(settings),
        "schedule": schedules.describe(settings, row),
        "retention": settings.backup_retention,
        "folder": str(settings.backup_dir),
    }


@backups_router.post("/backups", status_code=202)
def back_up_now(db: Session = Depends(get_db), actor: Principal = Depends(admin)) -> dict[str, Any]:
    job = queue.enqueue(
        db, schedules.BACKUP_JOB, {"requestedBy": str(actor.user_id)}, dedupe_key="backup",
        created_by=actor.user_id,
    )  # fmt: skip
    audit.record(db, action="backup.requested", actor=actor, target_type="backup")
    db.commit()
    return datasets.job_dict(job)


email_router = APIRouter(prefix="/api/admin")


class TestEmailIn(BaseModel):
    to: str | None = Field(default=None, max_length=320)


@email_router.post("/email/test")
def send_test_email(
    body: TestEmailIn | None = None,
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(admin),
) -> dict[str, Any]:
    """Send a test email, to the admin unless another address is given."""
    to = (body.to if body and body.to else actor.email) or ""
    if "@" not in to:
        raise ApiError(422, "invalid_email", "Give an address to send the test email to.")
    try:
        mail.send(settings, to, mail.test_message(settings))
    except mail.MailError as exc:
        raise ApiError(502, "email_failed", exc.message) from None
    return {"sentTo": to}


subscriptions_router = APIRouter(prefix="/api")


@subscriptions_router.get("/dashboards/{dashboard_id}/subscription")
def get_subscription(
    dashboard_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(view),
) -> dict[str, Any]:
    """Whether the caller has this dashboard emailed to them, and when."""
    return subs.get(db, settings, principal, dashboards.get(db, dashboard_id))


@subscriptions_router.put("/dashboards/{dashboard_id}/subscription")
def set_subscription(
    dashboard_id: uuid.UUID,
    body: ScheduleIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(view),
) -> dict[str, Any]:
    """Email this dashboard to the caller on a schedule; cron null stops it."""
    return subs.put(db, settings, principal, dashboards.get(db, dashboard_id), body.cron)


@subscriptions_router.get("/me/subscriptions")
def my_subscriptions(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(view),
) -> dict[str, Any]:
    return {
        "subscriptions": subs.mine(db, settings, principal),
        "presets": list(schedules.PRESETS),
        "timezone": settings.workspace_timezone,
    }


demo_router = APIRouter(prefix="/api/admin")


@demo_router.get("/demo")
def demo_status(
    db: Session = Depends(get_db), principal: Principal = Depends(admin)
) -> dict[str, Any]:
    """Whether the demo data is loaded, and its dashboard."""
    found = db.scalar(select(Dashboard).where(Dashboard.title == demo.TITLE))
    return {"loaded": found is not None, "dashboardId": str(found.id) if found else None}


@demo_router.post("/demo", status_code=202)
def load_demo(db: Session = Depends(get_db), actor: Principal = Depends(admin)) -> dict[str, Any]:
    """Load the demo datasets and dashboard, in the job runner."""
    job = queue.enqueue(
        db, demo.DEMO_JOB, {"requestedBy": str(actor.user_id)}, dedupe_key="demo",
        created_by=actor.user_id,
    )  # fmt: skip
    audit.record(db, action="demo.requested", actor=actor, target_type="demo")
    db.commit()
    return datasets.job_dict(job)
