"""Datasets: creation from a stream, refresh jobs, preview and deletion."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.connectors.base import RefreshMode
from opsis.connectors.convert import OVERRIDE_TYPES, check_format
from opsis.db.models import Connection, Dataset, Job, Snapshot
from opsis.errors import ApiError
from opsis.jobs import queue
from opsis.services import audit, connections, schedules
from opsis.settings import Settings
from opsis.storage import snapshots

NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")  # same as spec identifier
REFRESH_JOB = "dataset.refresh"
MAX_PREVIEW_ROWS = 1000


def suggest_name(stream: str) -> str:
    """A valid dataset identifier from a table or sheet name."""
    base = re.sub(r"[^a-z0-9]+", "_", stream.split(".")[-1].lower()).strip("_") or "dataset"
    if base[0].isdigit():
        base = f"t_{base}"
    return base[:63]


def get(db: Session, dataset_id: uuid.UUID) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ApiError(404, "not_found", "Dataset not found.")
    return dataset


def hide_samples(report: dict[str, Any]) -> dict[str, Any]:
    """A refresh report without the raw sample values, which are the source's data."""
    emptied = [{**e, "samples": [], "samplesHidden": True} for e in report.get("emptied", [])]
    return {**report, "emptied": emptied}


def samples_hidden_from(
    db: Session, settings: Settings, principal: Principal, dataset_ids: set[str]
) -> set[str]:
    """Which of these datasets (ids as text) show `principal` their refresh reports
    without sample values: restricted datasets they may not use, and datasets where
    row-level security hides rows from them (a sample could come from such a row)."""
    from opsis.services import access
    from opsis.services import query as queries  # avoids an import cycle

    wanted = []
    for value in dataset_ids:
        try:
            wanted.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not wanted:
        return set()
    names = {
        str(i): n
        for i, n in db.execute(select(Dataset.id, Dataset.name).where(Dataset.id.in_(wanted)))
    }
    blocked = access.blocked_datasets(db, principal)
    user = queries.user_context(principal, None)
    workspace = queries.load_workspace(db, settings)
    limited = {
        name
        for name in set(names.values()) - blocked
        if name in workspace.sources
        and queries.rls_applies(workspace, workspace.sources[name], user)
    }
    return {i for i, name in names.items() if name in blocked or name in limited}


def _quality(result: Any, *, brief: bool, hidden: bool) -> Any:
    report = result.get("dataQuality") if isinstance(result, dict) else None
    if not isinstance(report, dict):
        return result
    if brief:  # lists: the count alone
        return {**result, "dataQuality": {"emptiedValues": report.get("emptiedValues", 0)}}
    return {**result, "dataQuality": hide_samples(report)} if hidden else result


def job_dict(job: Job, *, hidden: set[str] | None = None, brief: bool = False) -> dict[str, Any]:
    """A job for the API. A refresh's result holds its data quality report: only
    its count when `brief`, and without sample values for the datasets in `hidden`
    (see samples_hidden_from)."""
    dataset_id = job.payload.get("dataset_id")
    return {
        "id": str(job.id),
        "type": job.type,
        "status": job.status,
        "datasetId": dataset_id,
        "full": bool(job.payload.get("full")),
        "attempts": job.attempts,
        "error": job.error,
        "result": _quality(job.result, brief=brief, hidden=dataset_id in (hidden or set())),
        "createdAt": job.created_at.isoformat(),
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
    }


def snapshot_dict(snapshot: Snapshot, current: bool, *, report: bool = False) -> dict[str, Any]:
    found = snapshot.report or {}
    described = {
        "id": str(snapshot.id),
        "rowCount": snapshot.row_count,
        "sizeBytes": snapshot.size_bytes,
        "mode": snapshot.mode,
        "columns": snapshot.columns,
        # Values the refresh stored as NULL because they did not fit their column.
        "emptiedValues": found.get("emptiedValues", 0),
        "createdAt": snapshot.created_at.isoformat(),
        "current": current,
    }
    if report:
        # Per column: how many, why and samples; type suggestions. None for
        # snapshots made before reports existed.
        described["report"] = snapshot.report
    return described


def without_samples(found: dict[str, Any]) -> dict[str, Any]:
    """A serialized dataset (or snapshot) with its reports' sample values removed."""
    if found.get("report"):
        found = {**found, "report": hide_samples(found["report"])}
    snapshot = found.get("snapshot")
    if snapshot and snapshot.get("report"):
        found = {**found, "snapshot": {**snapshot, "report": hide_samples(snapshot["report"])}}
    for key in ("lastJob", "job"):
        job = found.get(key)
        if job:
            found = {
                **found,
                key: {**job, "result": _quality(job["result"], brief=False, hidden=True)},
            }
    return found


def _latest_jobs(db: Session, dataset_ids: list[uuid.UUID]) -> dict[str, Job]:
    if not dataset_ids:
        return {}
    key = Job.payload["dataset_id"].astext
    rows = db.scalars(
        select(Job)
        .where(Job.type == REFRESH_JOB, key.in_([str(i) for i in dataset_ids]))
        .order_by(key, Job.created_at.desc())
        .ext(distinct_on(key))
    ).all()
    return {str(job.payload["dataset_id"]): job for job in rows}


def serialize(
    dataset: Dataset,
    connection: Connection | None,
    snapshot: Snapshot | None,
    job: Job | None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """A dataset for the API. `full` adds the refresh report with its sample values:
    the caller hides them from those who may not see them (see samples_hidden_from)."""
    return {
        "id": str(dataset.id),
        "name": dataset.name,
        "label": dataset.label,
        "connectionId": str(dataset.connection_id),
        "connectionName": connection.name if connection else None,
        "connectorId": connection.connector_id if connection else None,
        "stream": dataset.stream,
        "refreshMode": dataset.refresh_mode,
        "cursorField": dataset.cursor_field,
        "primaryKey": dataset.primary_key,
        "columns": dataset.columns,
        "columnOverrides": dataset.column_overrides or {},
        "restricted": dataset.restricted,
        "createdBy": str(dataset.created_by) if dataset.created_by else None,
        "snapshot": snapshot_dict(snapshot, True, report=full) if snapshot else None,
        "lastJob": job_dict(job, brief=not full) if job else None,
        "createdAt": dataset.created_at.isoformat(),
    }


def list_datasets(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Dataset, Connection, Snapshot)
        .join(Connection, Connection.id == Dataset.connection_id)
        .outerjoin(Snapshot, Snapshot.id == Dataset.current_snapshot_id)
        .order_by(Dataset.name)
    ).all()
    jobs = _latest_jobs(db, [row[0].id for row in rows])
    return [serialize(d, c, s, jobs.get(str(d.id))) for d, c, s in rows]


def detail(db: Session, dataset: Dataset) -> dict[str, Any]:
    connection = db.get(Connection, dataset.connection_id)
    current = db.get(Snapshot, dataset.current_snapshot_id) if dataset.current_snapshot_id else None
    job = _latest_jobs(db, [dataset.id]).get(str(dataset.id))
    return serialize(dataset, connection, current, job, full=True)


def create(
    db: Session,
    settings: Settings,
    actor: Principal,
    *,
    name: str,
    label: str | None,
    connection_id: uuid.UUID,
    stream: str,
    refresh_mode: str,
    cursor_field: str | None,
    primary_key: list[str],
    columns: list[str] | None,
) -> Dataset:
    details: list[dict[str, Any]] = []
    if not NAME_RE.match(name):
        details.append(
            {
                "path": "/name",
                "message": "Use lower-case letters, digits and underscores, starting with a "
                "letter (for example gifts or donor_gifts).",
            }
        )
    elif db.scalar(select(Dataset).where(Dataset.name == name)) is not None:
        raise ApiError(409, "name_taken", f"A dataset called {name!r} already exists.")

    connection = connections.get(db, connection_id)
    spec = next((s for s in connections.discover(settings, connection) if s.name == stream), None)
    if spec is None:
        raise ApiError(422, "invalid_request", f"Table {stream!r} was not found in the source.")
    available = {c.name for c in spec.columns}
    try:
        mode = RefreshMode(refresh_mode)
    except ValueError:
        mode = None
    if mode is None or mode not in spec.supported_modes:
        supported = ", ".join(m.value for m in spec.supported_modes)
        details.append({"path": "/refreshMode", "message": f"This table supports: {supported}."})
    if mode in (RefreshMode.APPEND, RefreshMode.MERGE):
        if cursor_field not in spec.cursor_candidates:
            details.append(
                {
                    "path": "/cursorField",
                    "message": "Choose a cursor column: "
                    + (", ".join(spec.cursor_candidates) or "none available")
                    + ".",
                }
            )
    else:
        cursor_field = None
    if mode == RefreshMode.MERGE:
        primary_key = primary_key or list(spec.primary_key)
        if not primary_key or not set(primary_key) <= available:
            details.append(
                {"path": "/primaryKey", "message": "Merge refresh needs a primary key column."}
            )
    else:
        primary_key = []
    if columns is not None:
        unknown = [c for c in columns if c not in available]
        if unknown or not columns:
            details.append(
                {
                    "path": "/columns",
                    "message": f"Unknown columns: {', '.join(unknown) or 'none selected'}.",
                }
            )
    if details:
        raise ApiError(422, "invalid_dataset", "The dataset settings are not valid.", details)

    dataset = Dataset(
        name=name,
        label=(label or "").strip() or stream,
        connection_id=connection.id,
        stream=stream,
        refresh_mode=refresh_mode,
        cursor_field=cursor_field,
        primary_key=primary_key,
        columns=columns,
        created_by=actor.user_id,
    )
    db.add(dataset)
    db.flush()
    audit.record(
        db,
        action="dataset.created",
        actor=actor,
        target_type="dataset",
        target_id=str(dataset.id),
        detail={"name": name, "stream": stream, "mode": refresh_mode},
    )
    db.commit()
    return dataset


def request_refresh(db: Session, actor: Principal, dataset: Dataset, *, full: bool = False) -> Job:
    """Queue a refresh. A dataset has at most one queued or running refresh."""
    payload: dict[str, Any] = {"dataset_id": str(dataset.id)}
    if full:
        payload["full"] = True
    return queue.enqueue(
        db,
        REFRESH_JOB,
        payload,
        dedupe_key=f"dataset:{dataset.id}",
        created_by=actor.user_id,
    )


def refresh_now(db: Session, settings: Settings, actor: Principal, dataset: Dataset) -> Job:
    """Run a refresh here and now, recorded as a job like any other (for data the app
    makes itself: the demo data and a date table). The job is returned finished, failed
    or not: its `error` says what went wrong."""
    from opsis.services import refresh  # it reads datasets through the connections

    job = Job(
        type=REFRESH_JOB,
        payload={"dataset_id": str(dataset.id)},
        status="running",
        locked_by="inline",
        started_at=datetime.now(UTC),
        attempts=1,
        created_by=actor.user_id,
    )
    db.add(job)
    db.commit()
    try:
        result = refresh.run_refresh(db, job, settings)
    except Exception as exc:
        db.rollback()
        queue.finish(db, job, error=str(exc)[:500])
        return job
    queue.finish(db, job, result=result)
    refresh.prune_snapshots(db, job, settings)
    return job


def set_overrides(
    db: Session, actor: Principal, dataset: Dataset, submitted: dict[str, Any]
) -> Job | None:
    """Replace the dataset's column types chosen by hand, and rebuild it with them.

    `submitted` maps a stored column name to {"type": ..., "format": ...}, or to
    None to go back to the source's type. Returns the full refresh queued, if any.
    """
    snapshot = (
        db.get(Snapshot, dataset.current_snapshot_id) if dataset.current_snapshot_id else None
    )
    if snapshot is None:
        raise ApiError(
            409, "no_data", "Refresh the dataset first: its columns are known after a refresh."
        )
    running = db.scalars(
        select(Job).where(Job.dedupe_key == f"dataset:{dataset.id}", Job.status == "running")
    ).first()
    if running is not None:
        raise ApiError(
            409,
            "refresh_running",
            "The dataset is refreshing now. Change the column types when it has finished.",
        )
    known = {c["name"] for c in snapshot.columns}
    overrides: dict[str, dict[str, Any]] = {}
    problems = []
    for name, chosen in submitted.items():
        if chosen is None:
            continue
        path = f"/overrides/{name}"
        if name not in known:
            problems.append({"path": path, "message": f"The dataset has no column {name!r}."})
            continue
        kind = chosen.get("type") if isinstance(chosen, dict) else None
        if kind not in OVERRIDE_TYPES:
            problems.append(
                {"path": path, "message": f"Choose one of: {', '.join(OVERRIDE_TYPES)}."}
            )
            continue
        entry: dict[str, Any] = {"type": kind}
        date_format = str(chosen.get("format") or "").strip()
        if date_format:
            if kind not in ("date", "datetime"):
                problems.append(
                    {"path": path, "message": "Only date and datetime columns take a format."}
                )
                continue
            try:
                entry["format"] = check_format(date_format)
            except ValueError as exc:
                problems.append({"path": path, "message": str(exc)})
                continue
        overrides[name] = entry
    if problems:
        raise ApiError(422, "invalid_overrides", "Some column types are not valid.", problems)
    before = dict(dataset.column_overrides or {})
    if overrides == before:
        return None
    dataset.column_overrides = overrides
    audit.record(
        db,
        action="dataset.column_types_changed",
        actor=actor,
        target_type="dataset",
        target_id=str(dataset.id),
        detail={"name": dataset.name, "before": before, "after": overrides},
    )
    db.commit()
    # The stored types change, so an incremental refresh could not add to the old files.
    job = request_refresh(db, actor, dataset, full=True)
    if job.status == "queued" and not job.payload.get("full"):
        job.payload = {**job.payload, "full": True}  # one was already waiting: rebuild instead
        db.commit()
    return job


def delete(db: Session, settings: Settings, actor: Principal, dataset: Dataset) -> None:
    dataset_id = dataset.id
    audit.record(
        db,
        action="dataset.deleted",
        actor=actor,
        target_type="dataset",
        target_id=str(dataset_id),
        detail={"name": dataset.name},
    )
    schedules.forget(db, dataset_id)
    from opsis.services import workspace_model  # avoids an import cycle

    # Its table and relationships go too, so a new dataset of the same name starts
    # afresh.
    workspace_model.remove_tables(
        db, [dataset.name], f"Removed {dataset.name}: the dataset was deleted"
    )
    db.delete(dataset)
    db.commit()
    snapshots.remove_dataset(settings, dataset_id)


def list_snapshots(db: Session, dataset: Dataset) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Snapshot)
        .where(Snapshot.dataset_id == dataset.id)
        .order_by(Snapshot.created_at.desc())
    ).all()
    return [snapshot_dict(s, s.id == dataset.current_snapshot_id) for s in rows]


def preview(
    db: Session,
    settings: Settings,
    dataset: Dataset,
    limit: int,
    *,
    principal: Principal | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """The first rows of the current snapshot.

    With a `principal`, row-level security is applied: when a rule restricts that
    user (the table's own, or one reaching it through a relationship), the rows are
    read through the same filtered scan as every query, so a preview never shows a
    hidden row.
    """
    limit = max(1, min(limit, MAX_PREVIEW_ROWS))
    snapshot = (
        db.get(Snapshot, dataset.current_snapshot_id) if dataset.current_snapshot_id else None
    )
    if snapshot is None:
        raise ApiError(
            409, "no_data", "This dataset has no data yet. Wait for the first refresh to finish."
        )
    path = snapshots.snapshot_dir(settings, dataset.id, snapshot.id)
    restricted = False
    rows: list[list[Any]] | None = None
    if principal is not None:
        from opsis.services import query as queries  # avoids an import cycle

        workspace = queries.load_workspace(db, settings)
        source = workspace.sources.get(dataset.name)
        user = queries.user_context(principal, None)
        if source is not None and queries.rls_applies(workspace, source, user):
            if runtime is None:
                raise ApiError(403, "forbidden", "Row-level security applies to this dataset.")
            db.rollback()
            rows = queries.secured_preview(settings, runtime, workspace, source, user, limit)
            restricted = True
    return {
        "snapshotId": str(snapshot.id),
        "columns": snapshot.columns,
        "rows": rows if rows is not None else snapshots.preview(path, limit),
        # With row-level security the total would reveal how much is hidden.
        "rowCount": None if restricted else snapshot.row_count,
        "limit": limit,
        "rlsApplied": restricted,
    }


def _hidden_for(
    db: Session, jobs: list[Job], settings: Settings | None, principal: Principal | None
) -> set[str]:
    if settings is None or principal is None:
        return set()
    reported = {
        str(job.payload["dataset_id"])
        for job in jobs
        if job.payload.get("dataset_id")
        and isinstance(job.result, dict)
        and job.result.get("dataQuality")
    }
    return samples_hidden_from(db, settings, principal, reported)


def list_jobs(
    db: Session,
    dataset_id: uuid.UUID | None,
    limit: int,
    *,
    settings: Settings | None = None,
    principal: Principal | None = None,
) -> list[dict[str, Any]]:
    """Recent jobs. With a principal, refresh reports of datasets they may not see
    in full lose their sample values."""
    query = select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 200)))
    if dataset_id is not None:
        query = query.where(Job.payload["dataset_id"].astext == str(dataset_id))
    jobs = list(db.scalars(query).all())
    hidden = _hidden_for(db, jobs, settings, principal)
    return [job_dict(job, hidden=hidden) for job in jobs]


def get_job(
    db: Session,
    job_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError(404, "not_found", "Job not found.")
    return job_dict(job, hidden=_hidden_for(db, [job], settings, principal))


def count_active_jobs(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(queue.ACTIVE))) or 0
