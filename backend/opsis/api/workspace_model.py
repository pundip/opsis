"""REST routes for the workspace model (ADR-031, V2_DESIGN 1.2): one model for the
workspace, with its tables, the relationships between them and the Model view's
layout. Everyone reads and changes only the part they may use."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.auth.principal import Permission, Principal
from opsis.errors import ApiError
from opsis.services import date_table, model_suggestions, model_warnings
from opsis.services import workspace_model as wm
from opsis.settings import Settings

router = APIRouter(prefix="/api/model")

read = require(Permission.DASHBOARDS_EDIT)
# POSTs that change nothing: an access token with the read scope may use them.
read_post = require(Permission.DASHBOARDS_EDIT, read_only=True)
write = require(Permission.MODELS_EDIT)


class DocumentIn(BaseModel):
    """The whole model as JSON (`document`) or YAML (`yaml`), not both."""

    document: dict[str, Any] | None = None
    yaml: str | None = None
    baseVersion: int | None = None  # the version you started from: see the module docs
    message: str | None = None  # a note for the version history


class PatchIn(BaseModel):
    """JSON Patch operations on the model as `GET /api/model` returns its `document`."""

    ops: list[dict[str, Any]]
    baseVersion: int | None = None
    message: str | None = None


class CheckIn(BaseModel):
    """A whole model (`document` or `yaml`) or a patch (`ops`) to check, not saved."""

    document: dict[str, Any] | None = None
    yaml: str | None = None
    ops: list[dict[str, Any]] | None = None
    baseVersion: int | None = None


class LayoutIn(BaseModel):
    layout: dict[str, Any]


class PairIn(BaseModel):
    """A field dragged onto another: each side a `table` and its `columns`."""

    model_config = ConfigDict(populate_by_name=True)
    dragged: dict[str, Any] = Field(alias="from")
    target: dict[str, Any] = Field(alias="to")


class SuggestIn(BaseModel):
    tables: list[str] | None = None  # only relationships that touch these


class DateTableIn(BaseModel):
    name: str | None = None  # the dataset and table name, dates by default
    label: str | None = None
    start: str | None = None  # the first date; by default from the model's data


def _described(
    db: Session, settings: Settings, request: Request, principal: Principal
) -> dict[str, Any]:
    """The model as the Model view shows it, with every warning (those that read
    the data too)."""
    described = wm.describe(db, settings, principal)
    described["warnings"] = model_warnings.check(
        db, settings, request.app.state.query_runtime, principal
    )
    return described


@router.get("")
def get_model(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read),
) -> dict[str, Any]:
    return _described(db, settings, request, principal)


@router.put("")
def put_model(
    body: DocumentIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    """Replace the part of the model you may use. A stale `baseVersion` is applied
    anyway when nobody changed the same places since; otherwise 409."""
    wm.save(
        db,
        settings,
        actor,
        document=wm.resolve_input(body.document, body.yaml),
        base_version=body.baseVersion,
        message=body.message,
    )
    return _described(db, settings, request, actor)


@router.patch("")
def patch_model(
    body: PatchIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    if not body.ops:
        raise ApiError(422, "invalid_patch", "Send at least one operation in `ops`.")
    wm.save(db, settings, actor, ops=body.ops, base_version=body.baseVersion, message=body.message)
    return _described(db, settings, request, actor)


@router.post("/validate")
def validate_model(
    body: CheckIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read_post),
) -> dict[str, Any]:
    """What a save would say: `problems` stop it, `existing` are problems the model
    already has (they do not stop a save), and `warnings` save with it."""
    if body.ops is not None and (body.document is not None or body.yaml is not None):
        raise ApiError(422, "invalid_request", "Send a document, YAML or `ops`, not more.")
    document = None if body.ops is not None else wm.resolve_input(body.document, body.yaml)
    checked, full = wm.validate(
        db, settings, principal, document=document, ops=body.ops, base_version=body.baseVersion
    )
    if full is not None:
        checked["warnings"] = model_warnings.check(
            db, settings, request.app.state.query_runtime, principal, full
        )
    return checked


@router.put("/layout")
def put_layout(
    body: LayoutIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    """Where the Model view draws the tables. Saved without a new version."""
    return {"layout": wm.save_layout(db, settings, actor, body.layout)}


@router.post("/key-check")
def key_check(
    body: PairIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read_post),
) -> dict[str, Any]:
    """What a relationship from one field to another would be: which side is the one
    side, its cardinality from the keys' uniqueness, active or not, and the evidence.
    Saves nothing."""
    return model_suggestions.check_pair(
        db, settings, request.app.state.query_runtime, principal, body.dragged, body.target
    )


@router.post("/suggestions")
def suggest_relationships(
    body: SuggestIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read_post),
) -> dict[str, Any]:
    """Relationships the model does not have yet, with a confidence and the evidence.
    Saves nothing."""
    return model_suggestions.suggest(
        db, settings, request.app.state.query_runtime, principal, body.tables
    )


@router.post("/date-table", status_code=201)
def generate_date_table(
    body: DateTableIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    """Make a date table from the built-in date connector, refreshed daily, and mark
    it in the model. Relationships from date columns come back as proposals."""
    return date_table.generate(
        db,
        settings,
        request.app.state.query_runtime,
        actor,
        name=body.name,
        label=body.label,
        start=body.start,
    )


@router.get("/export", response_class=PlainTextResponse)
def export_model(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(read),
) -> PlainTextResponse:
    """The part of the model you may use, as one YAML file."""
    return PlainTextResponse(
        wm.describe(db, settings, principal)["yaml"],
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="workspace-model.yml"'},
    )


@router.get("/versions")
def list_versions(
    db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> list[dict[str, Any]]:
    return wm.list_versions(db, principal)


@router.get("/versions/{version}")
def get_version(
    version: int, db: Session = Depends(get_db), principal: Principal = Depends(read)
) -> dict[str, Any]:
    return wm.get_version(db, principal, version)


@router.get("/diff")
def diff_versions(
    first: int,
    second: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(read),
) -> dict[str, Any]:
    """What changed from version `first` to version `second` (0 is the empty model)."""
    return wm.diff(db, principal, first, second)


@router.post("/versions/{version}/restore")
def restore_version(
    version: int,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(write),
) -> dict[str, Any]:
    wm.restore(db, settings, actor, version)
    return _described(db, settings, request, actor)
