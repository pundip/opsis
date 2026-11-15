"""REST routes for dashboards, their versions, and the data behind each item."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import get_db, get_settings, require
from opsis.api.sse import sse_response
from opsis.auth.principal import Permission, Principal
from opsis.services import dashboards, events
from opsis.settings import Settings

router = APIRouter(prefix="/api")

# A random id the editor sends with its own writes, so it can ignore the events they cause.
ClientId = Header(default=None, alias="X-Opsis-Client", max_length=64)

view = require(Permission.DASHBOARDS_VIEW)
edit = require(Permission.DASHBOARDS_EDIT)
publish_permission = require(Permission.DASHBOARDS_PUBLISH)


class DashboardIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    spec: dict[str, Any] | None = None


class SpecIn(BaseModel):
    spec: dict[str, Any]
    baseVersion: int | None = None  # the version you started from; a stale one gives 409
    message: str | None = None


class ValidateIn(BaseModel):
    spec: Any


class PublishIn(BaseModel):
    version: int | None = None  # defaults to the newest version


class DataIn(BaseModel):
    itemId: str
    source: Literal["draft", "published", "inline"] = "draft"
    spec: dict[str, Any] | None = None  # with source "inline": the unsaved spec to preview
    state: dict[str, Any] | None = None  # {"filters": {...}, "cross": [...], "grains": {...}}


class OptionsIn(BaseModel):
    filterId: str
    source: Literal["draft", "published", "inline"] = "draft"
    spec: dict[str, Any] | None = None
    search: str | None = Field(default=None, max_length=200)


@router.get("/visuals")
def list_visuals(
    settings: Settings = Depends(get_settings), _: Principal = Depends(view)
) -> list[dict[str, Any]]:
    """Every visual's manifest: roles, config schema and guidance for the agent."""
    return list(dashboards.manifests(settings).values())


@router.get("/dashboards")
def list_dashboards(
    db: Session = Depends(get_db), principal: Principal = Depends(view)
) -> list[dict[str, Any]]:
    return dashboards.list_dashboards(db, principal)


@router.post("/dashboards", status_code=201)
def create_dashboard(
    body: DashboardIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
) -> dict[str, Any]:
    created = dashboards.create(db, settings, actor, title=body.title, spec=body.spec)
    return dashboards.detail(db, created, actor)


@router.post("/dashboards/validate")
def validate_spec(
    body: ValidateIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(edit),
) -> dict[str, Any]:
    found = dashboards.problems(db, settings, body.spec)
    return {"valid": not found, "problems": found}


@router.get("/dashboards/{dashboard_id}")
def get_dashboard(
    dashboard_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(view)
) -> dict[str, Any]:
    return dashboards.detail(db, dashboards.get(db, dashboard_id), principal)


@router.put("/dashboards/{dashboard_id}")
def save_dashboard(
    dashboard_id: uuid.UUID,
    body: SpecIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
    client_id: str | None = ClientId,
) -> dict[str, Any]:
    saved = dashboards.save(
        db,
        settings,
        actor,
        dashboard_id,
        body.spec,
        base_version=body.baseVersion,
        message=body.message,
        client_id=client_id,
    )
    return dashboards.detail(db, saved, actor)


@router.delete("/dashboards/{dashboard_id}", status_code=204)
def delete_dashboard(
    dashboard_id: uuid.UUID, db: Session = Depends(get_db), actor: Principal = Depends(edit)
) -> None:
    dashboards.delete(db, actor, dashboards.get(db, dashboard_id))


@router.get("/dashboards/{dashboard_id}/versions")
def list_versions(
    dashboard_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(edit)
) -> list[dict[str, Any]]:
    return dashboards.list_versions(db, dashboards.get(db, dashboard_id))


@router.get("/dashboards/{dashboard_id}/versions/{version}")
def get_version(
    dashboard_id: uuid.UUID,
    version: int,
    db: Session = Depends(get_db),
    _: Principal = Depends(edit),
) -> dict[str, Any]:
    return dashboards.get_version(db, dashboards.get(db, dashboard_id), version)


@router.post("/dashboards/{dashboard_id}/versions/{version}/restore")
def restore_version(
    dashboard_id: uuid.UUID,
    version: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
    client_id: str | None = ClientId,
) -> dict[str, Any]:
    restored = dashboards.restore(
        db, settings, actor, dashboards.get(db, dashboard_id), version, client_id=client_id
    )
    return dashboards.detail(db, restored, actor)


@router.post("/dashboards/{dashboard_id}/publish")
def publish_dashboard(
    dashboard_id: uuid.UUID,
    body: PublishIn | None = None,
    db: Session = Depends(get_db),
    actor: Principal = Depends(publish_permission),
    client_id: str | None = ClientId,
) -> dict[str, Any]:
    target = dashboards.get(db, dashboard_id)
    dashboards.publish(db, actor, target, body.version if body else None, client_id=client_id)
    return dashboards.detail(db, target, actor)


@router.post("/dashboards/{dashboard_id}/unpublish")
def unpublish_dashboard(
    dashboard_id: uuid.UUID,
    db: Session = Depends(get_db),
    actor: Principal = Depends(publish_permission),
) -> dict[str, Any]:
    target = dashboards.get(db, dashboard_id)
    dashboards.unpublish(db, actor, target)
    return dashboards.detail(db, target, actor)


@router.get("/dashboards/{dashboard_id}/events")
async def dashboard_events(
    dashboard_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(edit),
) -> StreamingResponse:
    """Server-sent events for an open editor: `version_created` and `published`.

    The first event is `hello` with the current version, so a client that
    reconnects can tell whether it missed anything.
    """
    current = dashboards.get(db, dashboard_id)
    hello = {"dashboardId": str(current.id), "version": current.version}
    # The stream can stay open for hours. Give the database connection back now.
    db.close()

    async def stream() -> AsyncIterator[tuple[str, Any]]:
        yield "hello", hello
        async for event in events.hub.subscribe(events.dashboard_channel(dashboard_id)):
            yield event

    return sse_response(stream(), settings.sse_keepalive_seconds)


@router.post("/dashboards/{dashboard_id}/data")
def item_data(
    dashboard_id: uuid.UUID,
    body: DataIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(view),
) -> dict[str, Any]:
    return dashboards.item_data(
        db,
        settings,
        request.app.state.query_runtime,
        principal,
        dashboards.get(db, dashboard_id),
        item_id=body.itemId,
        source=body.source,
        inline_spec=body.spec,
        state=body.state,
    )


@router.post("/dashboards/{dashboard_id}/filter-options")
def filter_options(
    dashboard_id: uuid.UUID,
    body: OptionsIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(view),
) -> dict[str, Any]:
    return dashboards.filter_options(
        db,
        settings,
        request.app.state.query_runtime,
        principal,
        dashboards.get(db, dashboard_id),
        filter_id=body.filterId,
        source=body.source,
        inline_spec=body.spec,
        search=body.search,
    )
