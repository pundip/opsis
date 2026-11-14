"""REST routes for semantic models and queries."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.auth.principal import Permission, Principal
from opsis.services import models, query
from opsis.settings import Settings

router = APIRouter(prefix="/api")

read = require(Permission.DASHBOARDS_EDIT)
write = require(Permission.MODELS_EDIT)


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
    db: Session = Depends(get_db), _: Principal = Depends(read)
) -> list[dict[str, Any]]:
    return models.list_models(db)


@router.get("/models/{dataset}")
def get_model(
    dataset: str, db: Session = Depends(get_db), _: Principal = Depends(read)
) -> dict[str, Any]:
    return models.describe(db, models.dataset_by_name(db, dataset))


@router.put("/models/{dataset}")
def save_model(
    dataset: str,
    body: ModelIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    target = models.dataset_by_name(db, dataset)
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
    dataset: str, db: Session = Depends(get_db), _: Principal = Depends(read)
) -> list[dict[str, Any]]:
    return models.list_versions(db, models.dataset_by_name(db, dataset))


@router.get("/models/{dataset}/versions/{version}")
def get_model_version(
    dataset: str, version: int, db: Session = Depends(get_db), _: Principal = Depends(read)
) -> dict[str, Any]:
    return models.get_version(db, models.dataset_by_name(db, dataset), version)


@router.post("/models/{dataset}/versions/{version}/restore")
def restore_model_version(
    dataset: str,
    version: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    target = models.dataset_by_name(db, dataset)
    models.restore(db, settings, actor, target, version)
    return models.describe(db, target)


@router.post("/models/{dataset}/validate")
def validate_model(
    dataset: str,
    body: ModelIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(read),
) -> dict[str, Any]:
    target = models.dataset_by_name(db, dataset)
    found = models.problems(db, settings, target, models.resolve_input(body.definition, body.yaml))
    return {"valid": not found, "problems": found}


@router.get("/models/{dataset}/starter")
def starter_model(
    dataset: str, db: Session = Depends(get_db), _: Principal = Depends(read)
) -> dict[str, Any]:
    """A freshly generated model for the dataset's current columns. Not saved."""
    target = models.dataset_by_name(db, dataset)
    definition = models.starter(target, models.current_columns(db, target))
    return {"definition": definition, "yaml": models.to_yaml(target.name, definition)}


@router.get("/models/{dataset}/export", response_class=PlainTextResponse)
def export_model(
    dataset: str, db: Session = Depends(get_db), _: Principal = Depends(read)
) -> PlainTextResponse:
    """The model as a YAML file, for version control (models/<dataset>.yml)."""
    target = models.dataset_by_name(db, dataset)
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
    principal: Principal = Depends(read),
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
