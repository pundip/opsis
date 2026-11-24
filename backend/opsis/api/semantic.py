"""REST routes for semantic models and queries."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.api.errors import ApiError
from opsis.auth.principal import Permission, Principal
from opsis.db.models import Dataset
from opsis.services import access, model_files, models, query
from opsis.settings import Settings

router = APIRouter(prefix="/api")

read = require(Permission.DASHBOARDS_EDIT)
# POSTs that change nothing: an access token with the read scope may use them.
read_post = require(Permission.DASHBOARDS_EDIT, read_only=True)
write = require(Permission.MODELS_EDIT)


def _dataset(db: Session, name: str, principal: Principal) -> Dataset:
    """A dataset by name, if the caller may use it (restricted datasets)."""
    target = models.dataset_by_name(db, name)
    access.require_datasets(db, principal, {target.name})
    return target


class ModelIn(BaseModel):
    """Send the model as JSON (`definition`) or as YAML text (`yaml`), not both."""

    definition: dict[str, Any] | None = None
    yaml: str | None = None
    baseVersion: int | None = None  # the version you started from; a stale one gives 409
    message: str | None = None  # a note for the version history


class QueryIn(BaseModel):
    query: dict[str, Any]
    previewAs: dict[str, Any] | None = None  # admin only: {"role": "viewer", "attributes": {...}}
    includeSql: bool = False


@router.get("/models")
def list_models(
    db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> list[dict[str, Any]]:
    blocked = access.blocked_datasets(db, principal)
    return [m for m in models.list_models(db) if m["dataset"] not in blocked]


@router.get("/model-files")
def export_models(db: Session = Depends(get_db), principal: Principal = Depends(read)) -> Response:
    """Every model the caller may use, as a zip of models/<dataset>.yml."""
    name, content = model_files.export_zip(db, principal)
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/model-files")
def import_models(
    files: list[UploadFile],
    dryRun: bool = False,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    """Import a zip of a models/ folder, or .yml files: all checked first, and
    saved only if every one is valid. With dryRun=true nothing is saved."""
    uploads, total = [], 0
    for upload in files:
        content = upload.file.read(model_files.MAX_UPLOAD_BYTES + 1)
        total += len(content)
        if total > model_files.MAX_UPLOAD_BYTES:
            raise ApiError(413, "too_large", "Send at most 20 MB of model files at once.")
        uploads.append((upload.filename or "model.yml", content))
    found = model_files.read_uploads(uploads)
    return model_files.import_files(db, settings, actor, found, dry_run=dryRun)


@router.get("/models/{dataset}")
def get_model(
    dataset: str, db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> dict[str, Any]:
    return models.describe(db, _dataset(db, dataset, principal))


@router.put("/models/{dataset}")
def save_model(
    dataset: str,
    body: ModelIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    target = _dataset(db, dataset, actor)
    models.save(
        db,
        settings,
        actor,
        target,
        models.resolve_input(body.definition, body.yaml),
        base_version=body.baseVersion,
        message=body.message,
    )
    return models.describe(db, target)


@router.get("/models/{dataset}/versions")
def list_model_versions(
    dataset: str, db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> list[dict[str, Any]]:
    return models.list_versions(db, _dataset(db, dataset, principal))


@router.get("/models/{dataset}/versions/{version}")
def get_model_version(
    dataset: str, version: int, db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> dict[str, Any]:
    return models.get_version(db, _dataset(db, dataset, principal), version)


@router.post("/models/{dataset}/versions/{version}/restore")
def restore_model_version(
    dataset: str,
    version: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    target = _dataset(db, dataset, actor)
    models.restore(db, settings, actor, target, version)
    return models.describe(db, target)


@router.post("/models/{dataset}/validate")
def validate_model(
    dataset: str,
    body: ModelIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read_post),
) -> dict[str, Any]:
    target = _dataset(db, dataset, principal)
    found = models.problems(db, settings, target, models.resolve_input(body.definition, body.yaml))
    return {"valid": not found, "problems": found}


@router.get("/models/{dataset}/starter")
def starter_model(
    dataset: str, db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> dict[str, Any]:
    """A freshly generated model for the dataset's current columns. Not saved."""
    target = _dataset(db, dataset, principal)
    definition = models.starter(target, models.current_columns(db, target))
    return {"definition": definition, "yaml": models.to_yaml(target.name, definition)}


@router.get("/models/{dataset}/export", response_class=PlainTextResponse)
def export_model(
    dataset: str, db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> PlainTextResponse:
    """The model as a YAML file, for version control (models/<dataset>.yml)."""
    target = _dataset(db, dataset, principal)
    return PlainTextResponse(
        models.describe(db, target)["yaml"],
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{target.name}.yml"'},
    )


@router.post("/query")
def run_query(
    body: QueryIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read_post),
) -> dict[str, Any]:
    return query.run(
        db,
        settings,
        request.app.state.query_runtime,
        principal,
        body.query,
        preview_as=body.previewAs,
        include_sql=body.includeSql,
    )
