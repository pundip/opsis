"""Routes for sharing: dashboard access lists, dataset access, the directory of
people and groups (sign-in modes), and share links (behind their flag), plus
the public side of share links."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from opsis.api.deps import client_ip, get_db, get_settings, require
from opsis.auth import cookies
from opsis.auth.principal import Permission, Principal
from opsis.errors import ApiError
from opsis.services import access, dashboards, datasets, links
from opsis.settings import Settings

router = APIRouter(prefix="/api")
links_router = APIRouter(prefix="/api")
public_router = APIRouter(prefix="/api/public")

edit = require(Permission.DASHBOARDS_EDIT)
publish = require(Permission.DASHBOARDS_PUBLISH)
build = require(Permission.DATASETS_EDIT)


class ShareIn(BaseModel):
    grantee: str = Field(min_length=1, max_length=60)
    role: Literal["viewer", "editor"]


class SharesIn(BaseModel):
    shares: list[ShareIn] = Field(max_length=500)


class DatasetAccessIn(BaseModel):
    restricted: bool
    grants: list[str] = Field(default_factory=list, max_length=500)


class LinkIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    expiresInDays: int | None = None
    password: str | None = Field(default=None, max_length=200)
    # None or {} (deny everything under row-level security), "mine", or an object (admins).
    attributes: dict[str, Any] | Literal["mine"] | None = None


class UnlockIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class PublicDataIn(BaseModel):
    itemId: str = Field(max_length=64)
    state: dict[str, Any] | None = None


class PublicOptionsIn(BaseModel):
    filterId: str = Field(max_length=64)
    search: str | None = Field(default=None, max_length=200)


@router.get("/directory")
def directory(db: Session = Depends(get_db), _: Principal = Depends(edit)) -> dict[str, Any]:
    """People and groups, to share with."""
    return access.directory(db)


@router.get("/dashboards/{dashboard_id}/access")
def dashboard_access(
    dashboard_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(edit)
) -> dict[str, Any]:
    dashboard = dashboards.get(db, dashboard_id)
    access.require_dashboard(db, dashboard, principal, "share")
    return access.dashboard_shares(db, dashboard, principal)


@router.put("/dashboards/{dashboard_id}/access")
def set_dashboard_access(
    dashboard_id: uuid.UUID,
    body: SharesIn,
    db: Session = Depends(get_db),
    actor: Principal = Depends(edit),
) -> dict[str, Any]:
    return access.set_dashboard_shares(
        db, actor, dashboards.get(db, dashboard_id), [s.model_dump() for s in body.shares]
    )


@router.get("/datasets/{dataset_id}/access")
def dataset_access(
    dataset_id: uuid.UUID, db: Session = Depends(get_db), principal: Principal = Depends(build)
) -> dict[str, Any]:
    dataset = datasets.get(db, dataset_id)
    access.require_datasets(db, principal, {dataset.name})
    return access.dataset_grants(db, dataset, principal)


@router.put("/datasets/{dataset_id}/access")
def set_dataset_access(
    dataset_id: uuid.UUID,
    body: DatasetAccessIn,
    db: Session = Depends(get_db),
    actor: Principal = Depends(build),
) -> dict[str, Any]:
    """The dataset's owner (its creator) and admins; see services/access.py."""
    return access.set_dataset_grants(
        db, actor, datasets.get(db, dataset_id), body.restricted, body.grants
    )


# ---- share links: managing ------------------------------------------------------------


@links_router.get("/dashboards/{dashboard_id}/links")
def list_links(
    dashboard_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(edit),
) -> list[dict[str, Any]]:
    return links.list_links(db, settings, principal, dashboards.get(db, dashboard_id))


@links_router.post("/dashboards/{dashboard_id}/links", status_code=201)
def create_link(
    dashboard_id: uuid.UUID,
    body: LinkIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(publish),
) -> dict[str, Any]:
    """The link's URL is returned once."""
    described, url = links.create(
        db,
        settings,
        actor,
        dashboards.get(db, dashboard_id),
        name=body.name,
        expires_in_days=body.expiresInDays,
        password=body.password,
        attributes=body.attributes,
    )
    return {**described, "url": url}


@links_router.post("/links/{link_id}/revoke")
def revoke_link(
    link_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: Principal = Depends(edit),
) -> dict[str, Any]:
    return links.revoke(db, settings, actor, link_id)


# ---- share links: the public side ---------------------------------------------------------


def _open(db: Session, settings: Settings, request: Request, token: str) -> Any:
    link, dashboard = links.resolve(db, settings, token)
    if not links.unlocked(settings, link, cookies.cookie_values(request, links.cookie_name(link))):
        raise ApiError(401, "password_required", "This link needs a password.")
    return link, dashboard


@public_router.get("/links/{token}")
def open_link(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The published dashboard behind a link. Counts a view."""
    link, dashboard = links.resolve(db, settings, token)
    if not links.unlocked(settings, link, cookies.cookie_values(request, links.cookie_name(link))):
        return {"needsPassword": True}
    spec = dashboards.published_spec(db, dashboard)
    links.count_view(db, link)
    return {"needsPassword": False, "title": spec.get("title"), "spec": spec}


@public_router.post("/links/{token}/unlock")
def unlock_link(
    token: str,
    body: UnlockIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    link, _ = links.resolve(db, settings, token)
    value = links.unlock(db, settings, link, body.password, client_ip(request))
    response.set_cookie(
        links.cookie_name(link),
        value,
        max_age=links.UNLOCK_HOURS * 3600,
        **cookies.cookie_attrs(settings, http_only=True),
    )
    return {"unlocked": True}


@public_router.post("/links/{token}/data")
def link_data(
    token: str,
    body: PublicDataIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    link, dashboard = _open(db, settings, request, token)
    spec = dashboards.published_spec(db, dashboard)
    principal = links.principal_for(link)
    user = dashboards.viewer_context(db, principal, None)
    return dashboards.data_for_spec(
        db,
        settings,
        request.app.state.query_runtime,
        spec,
        body.itemId,
        body.state,
        user=user,
        include_query=False,
    )


@public_router.post("/links/{token}/filter-options")
def link_filter_options(
    token: str,
    body: PublicOptionsIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    link, dashboard = _open(db, settings, request, token)
    spec = dashboards.published_spec(db, dashboard)
    user = dashboards.viewer_context(db, links.principal_for(link), None)
    return dashboards.options_for_spec(
        db, settings, request.app.state.query_runtime, spec, body.filterId, body.search, user=user
    )
