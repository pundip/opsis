"""The dataset refresh job: source -> Arrow batches -> Parquet snapshot -> pointer swap.

Runs inside the api (JOB_RUNNER=inline) or the worker container, through the
same function. The new snapshot, the cursor state and the job status are
committed in one transaction, so a failed refresh changes nothing that readers
or the next refresh can see.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.connectors.base import ConnectorError, ReadOptions, RefreshMode
from opsis.db.models import Connection, Dataset, Job, Snapshot
from opsis.errors import JobError
from opsis.services import connections, models
from opsis.settings import Settings
from opsis.storage import snapshots

log = logging.getLogger("opsis.refresh")


class RefreshError(JobError):
    """A failure with a message that is safe and useful to show in the job."""


def run_refresh(db: Session, job: Job, settings: Settings) -> dict[str, Any]:
    started = time.monotonic()
    dataset_id = uuid.UUID(str(job.payload["dataset_id"]))
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise RefreshError("The dataset no longer exists.")
    connection = db.get(Connection, dataset.connection_id)
    if connection is None:
        raise RefreshError("The dataset's connection no longer exists.")

    previous_id = dataset.current_snapshot_id
    mode = RefreshMode(dataset.refresh_mode)
    if job.payload.get("full") or previous_id is None:
        mode = RefreshMode.FULL  # first run, or the user asked to rebuild
    state = dataset.state if mode != RefreshMode.FULL else None
    options = ReadOptions(
        columns=tuple(dataset.columns) if dataset.columns else None,
        cursor_field=dataset.cursor_field,
        primary_key=tuple(dataset.primary_key),
    )
    # The configured mode decides whether a cursor is tracked, even on a full run,
    # so the next incremental refresh knows where to continue.
    read_mode = mode
    if mode == RefreshMode.FULL and dataset.refresh_mode != RefreshMode.FULL.value:
        read_mode = RefreshMode(dataset.refresh_mode)
    stream, primary_key = dataset.stream, tuple(dataset.primary_key)
    previous = snapshots.snapshot_dir(settings, dataset_id, previous_id) if previous_id else None
    db.commit()  # do not hold a transaction open during a long read

    try:
        connector = connections.instantiate(settings, connection)
    except ConnectorError as exc:
        raise RefreshError(str(exc)) from exc
    try:
        result = connector.read(stream, read_mode, state, options)
        built = snapshots.build_snapshot(
            settings,
            dataset_id,
            result.batches,
            mode=mode,
            previous=previous,
            primary_key=primary_key,
        )
        new_state = result.final_state()
    except (ConnectorError, snapshots.SnapshotError) as exc:
        raise RefreshError(str(exc)) from exc
    finally:
        connector.close()

    # Swap the pointer. Committed by the caller together with the job status.
    locked = db.scalar(select(Dataset).where(Dataset.id == dataset_id).with_for_update())
    if locked is None:
        snapshots.remove_snapshot(settings, dataset_id, built.snapshot_id)
        raise RefreshError("The dataset was deleted while it was refreshing.")
    db.add(
        Snapshot(
            id=built.snapshot_id,
            dataset_id=dataset_id,
            row_count=built.row_count,
            size_bytes=built.size_bytes,
            columns=built.columns,
            mode=mode.value,
            job_id=job.id,
        )
    )
    locked.current_snapshot_id = built.snapshot_id
    if dataset.refresh_mode != RefreshMode.FULL.value:
        locked.state = new_state or locked.state
    # A new dataset gets a starter semantic model; an unedited starter follows the columns.
    models.ensure_starter(db, locked, {c["name"]: c["type"] for c in built.columns})
    db.flush()
    return {
        "snapshotId": str(built.snapshot_id),
        "rowCount": built.row_count,
        "sizeBytes": built.size_bytes,
        "mode": mode.value,
        "seconds": round(time.monotonic() - started, 2),
    }


def prune_snapshots(db: Session, job: Job, settings: Settings) -> None:
    """Keep the newest SNAPSHOT_RETENTION snapshots of the dataset, delete the rest."""
    dataset_id = uuid.UUID(str(job.payload["dataset_id"]))
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        return
    rows = db.scalars(
        select(Snapshot)
        .where(Snapshot.dataset_id == dataset_id)
        .order_by(Snapshot.created_at.desc())
    ).all()
    for old in rows[settings.snapshot_retention :]:
        if old.id == dataset.current_snapshot_id:
            continue
        db.delete(old)
        db.commit()  # row first: a directory without a row is harmless, the reverse is not
        snapshots.remove_snapshot(settings, dataset_id, old.id)
        log.info("Pruned snapshot %s of dataset %s", old.id, dataset.name)
