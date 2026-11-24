"""REST routes for connectors, connections, uploads, datasets and jobs.

Routers only translate HTTP to service calls. All rules live in opsis.services.
"""

from __future__ import annotations

import re
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.auth.principal import Permission, Principal
from opsis.connectors.file import ALLOWED_SUFFIXES
from opsis.db.models import Dataset
from opsis.errors import ApiError
from opsis.services import access, connections, datasets, schedules
from opsis.settings import Settings

router = APIRouter(prefix="/api")

manage = require(Permission.CONNECTIONS_MANAGE)
edit = require(Permission.DATASETS_EDIT)


# ---- Connectors and connections -------------------------------------------------


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connectorId: str
    config: dict[str, Any]


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None


class ConnectionTest(BaseModel):
    connectorId: str
    config: dict[str, Any]
    connectionId: uuid.UUID | None = None  # reuse saved secrets for fields left blank


@router.get("/connectors")
def list_connectors(
    settings: Settings = Depends(get_settings), _: Principal = Depends(edit)
) -> list[dict[str, Any]]:
    return connections.list_connectors(settings)


@router.get("/connections")
def list_connections(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(edit),
) -> list[dict[str, Any]]:
    return connections.list_connections(db, settings)


@router.post("/connections", status_code=201)
def create_connection(
    body: ConnectionIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    connection = connections.create(
        db, settings, actor, name=body.name, connector_id=body.connectorId, config=body.config
    )
    return connections.serialize(settings, connection)


@router.post("/connections/test")
def test_connection_settings(
    body: ConnectionTest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(manage),
) -> dict[str, Any]:
    return connections.test_unsaved(db, settings, body.connectorId, body.config, body.connectionId)


@router.get("/connections/{connection_id}")
def get_connection(
    connection_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(edit),
) -> dict[str, Any]:
    return connections.serialize(settings, connections.get(db, connection_id))


@router.patch("/connections/{connection_id}")
def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionPatch,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> dict[str, Any]:
    connection = connections.update(
        db, settings, actor, connections.get(db, connection_id), name=body.name, config=body.config
    )
    return connections.serialize(settings, connection)


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(
    connection_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(manage),
) -> None:
    connections.delete(db, settings, actor, connections.get(db, connection_id))


@router.post("/connections/{connection_id}/test")
def test_saved_connection(
    connection_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(manage),
) -> dict[str, Any]:
    return connections.test_saved(db, settings, connections.get(db, connection_id))


@router.get("/connections/{connection_id}/streams")
def list_streams(
    connection_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(edit),
) -> list[dict[str, Any]]:
    streams = connections.discover(settings, connections.get(db, connection_id))
    return [
        {**connections.stream_dict(s), "suggestedName": datasets.suggest_name(s.name)}
        for s in streams
    ]


# ---- Uploads --------------------------------------------------------------------


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name.replace("\\", "/").split("/")[-1]).strip(" .")
    return name[:150] or "upload"


@router.post("/uploads", status_code=201)
def upload_file(
    request: Request,
    file: UploadFile,
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(manage),
) -> dict[str, Any]:
    """Store an uploaded CSV or Excel file. The returned `file` goes into a
    file connection's settings."""
    limit = settings.max_upload_mb * 1024 * 1024
    too_big = ApiError(
        413, "file_too_large", f"The file is larger than the {settings.max_upload_mb} MB limit."
    )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise too_big
    filename = _safe_filename(file.filename or "")
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise ApiError(422, "unsupported_file", f"Upload one of these file types: {allowed}.")

    upload_id = uuid.uuid4().hex
    folder = connections.uploads_dir(settings) / upload_id
    folder.mkdir(parents=True)
    size = 0
    try:
        with (folder / filename).open("wb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise too_big
                target.write(chunk)
        if size == 0:
            raise ApiError(422, "empty_file", "The file is empty.")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"file": f"{upload_id}/{filename}", "filename": filename, "sizeBytes": size}


# ---- Datasets -------------------------------------------------------------------


class DatasetIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    label: str | None = Field(default=None, max_length=200)
    connectionId: uuid.UUID
    stream: str = Field(min_length=1, max_length=300)
    refreshMode: str = "full"
    cursorField: str | None = None
    primaryKey: list[str] = []
    columns: list[str] | None = None
    refresh: bool = True  # queue the first refresh straight away


class RefreshIn(BaseModel):
    full: bool = False  # rebuild from scratch, ignoring the incremental cursor


def _allowed(db: Session, dataset_id: uuid.UUID, principal: Principal) -> Dataset:
    """A dataset, if the caller may use it (restricted datasets)."""
    dataset = datasets.get(db, dataset_id)
    access.require_datasets(db, principal, {dataset.name})
    return dataset


@router.get("/datasets")
def list_datasets(
    db: Session = Depends(get_db), principal: Principal = Depends(edit)
) -> list[dict[str, Any]]:
    blocked = access.blocked_datasets(db, principal)
    return [d for d in datasets.list_datasets(db) if d["name"] not in blocked]


@router.post("/datasets", status_code=201)
def create_dataset(
    body: DatasetIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
) -> dict[str, Any]:
    dataset = datasets.create(
        db,
        settings,
        actor,
        name=body.name,
        label=body.label,
        connection_id=body.connectionId,
        stream=body.stream,
        refresh_mode=body.refreshMode,
        cursor_field=body.cursorField,
        primary_key=body.primaryKey,
        columns=body.columns,
    )
    if body.refresh:
        datasets.request_refresh(db, actor, dataset)
    return datasets.detail(db, dataset)


@router.get("/datasets/{dataset_id}")
def get_dataset(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(edit),
) -> dict[str, Any]:
    dataset = _allowed(db, dataset_id, principal)
    found = datasets.detail(db, dataset)
    if settings.feature_scheduled_refresh:
        found["schedule"] = schedules.refresh_schedule(db, settings, dataset)
    return found


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
) -> None:
    datasets.delete(db, settings, actor, _allowed(db, dataset_id, actor))


@router.post("/datasets/{dataset_id}/refresh", status_code=202)
def refresh_dataset(
    dataset_id: uuid.UUID,
    body: RefreshIn | None = None,
    db: Session = Depends(get_db),
    actor: Principal = Depends(edit),
) -> dict[str, Any]:
    job = datasets.request_refresh(
        db, actor, _allowed(db, dataset_id, actor), full=bool(body and body.full)
    )
    return datasets.job_dict(job)


@router.get("/datasets/{dataset_id}/preview")
def preview_dataset(
    dataset_id: uuid.UUID,
    request: Request,
    limit: int = 100,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(edit),
) -> dict[str, Any]:
    return datasets.preview(
        db,
        settings,
        _allowed(db, dataset_id, principal),
        limit,
        principal=principal,
        runtime=request.app.state.query_runtime,
    )


@router.get("/datasets/{dataset_id}/snapshots")
def list_snapshots(
    dataset_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(edit)
) -> list[dict[str, Any]]:
    return datasets.list_snapshots(db, _allowed(db, dataset_id, principal))


# ---- Jobs -----------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(
    datasetId: uuid.UUID | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    _: Principal = Depends(edit),
) -> list[dict[str, Any]]:
    return datasets.list_jobs(db, datasetId, limit)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(edit)
) -> dict[str, Any]:
    return datasets.get_job(db, job_id)
