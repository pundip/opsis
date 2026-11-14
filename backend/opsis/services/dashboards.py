"""Dashboards: versions, draft and published states, and the data behind each item."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Permission, Principal
from opsis.dashboards import spec as specs
from opsis.dashboards.manifests import Manifest, load_manifests
from opsis.db.models import Dashboard, DashboardVersion, Dataset, User
from opsis.errors import ApiError
from opsis.semantic.model import Model, Problem, schema_problems, schema_validator
from opsis.services import audit
from opsis.services import query as queries
from opsis.services.query import QueryRuntime
from opsis.settings import Settings

MAX_FILTER_OPTIONS = 500


def manifests(settings: Settings) -> dict[str, Manifest]:
    return load_manifests(settings.visuals_dir, settings.spec_dir)


def all_models(db: Session, settings: Settings) -> dict[str, Model]:
    """The semantic model of every dataset that has data."""
    names = set(db.scalars(select(Dataset.name)).all())
    return {
        name: source.model for name, source in queries.load_sources(db, settings, names).items()
    }


def problems(db: Session, settings: Settings, spec: Any) -> list[Problem]:
    return specs.validate(settings.spec_dir, spec, manifests(settings), all_models(db, settings))


def get(db: Session, dashboard_id: uuid.UUID) -> Dashboard:
    dashboard = db.get(Dashboard, dashboard_id)
    if dashboard is None:
        raise ApiError(404, "not_found", "Dashboard not found.")
    return dashboard


def _version_row(db: Session, dashboard: Dashboard, number: int) -> DashboardVersion:
    row = db.scalar(
        select(DashboardVersion).where(
            DashboardVersion.dashboard_id == dashboard.id, DashboardVersion.version == number
        )
    )
    if row is None:
        raise ApiError(404, "not_found", f"There is no version {number} of this dashboard.")
    return row


def can_edit(principal: Principal) -> bool:
    return principal.can(Permission.DASHBOARDS_EDIT)


def spec_for(
    db: Session, dashboard: Dashboard, principal: Principal, source: str
) -> dict[str, Any]:
    """The spec a caller may see. Only editors can read the draft."""
    if source == "draft" and can_edit(principal):
        return _version_row(db, dashboard, dashboard.version).spec
    if dashboard.published_version is None:
        raise ApiError(404, "not_published", "This dashboard has not been published yet.")
    return _version_row(db, dashboard, dashboard.published_version).spec


def summary(dashboard: Dashboard, owner: str | None = None) -> dict[str, Any]:
    return {
        "id": str(dashboard.id),
        "title": dashboard.title,
        "version": dashboard.version,
        "publishedVersion": dashboard.published_version,
        "hasUnpublishedChanges": dashboard.published_version != dashboard.version,
        "ownerName": owner,
        "createdAt": dashboard.created_at.isoformat(),
        "updatedAt": dashboard.updated_at.isoformat(),
    }


def list_dashboards(db: Session, principal: Principal) -> list[dict[str, Any]]:
    query = (
        select(Dashboard, User.display_name)
        .outerjoin(User, User.id == Dashboard.owner_id)
        .order_by(func.lower(Dashboard.title))
    )
    if not can_edit(principal):
        query = query.where(Dashboard.published_version.is_not(None))
    return [summary(d, owner) for d, owner in db.execute(query).all()]


def detail(db: Session, dashboard: Dashboard, principal: Principal) -> dict[str, Any]:
    """The dashboard as this caller sees it: editors get the draft, others the published version."""
    source = "draft" if can_edit(principal) else "published"
    owner = db.scalar(select(User.display_name).where(User.id == dashboard.owner_id))
    return {
        **summary(dashboard, owner),
        "source": source,
        "spec": spec_for(db, dashboard, principal, source),
        "canEdit": can_edit(principal),
        "canPublish": principal.can(Permission.DASHBOARDS_PUBLISH),
    }


def _write_version(
    db: Session,
    dashboard: Dashboard,
    spec: dict[str, Any],
    author_type: str,
    author_id: uuid.UUID | None,
    message: str | None,
) -> None:
    dashboard.version += 1
    dashboard.title = spec["title"]
    db.add(
        DashboardVersion(
            dashboard_id=dashboard.id,
            version=dashboard.version,
            spec=spec,
            author_type=author_type,
            author_id=author_id,
            message=(message or "").strip()[:500] or None,
        )
    )


def _checked(db: Session, settings: Settings, spec: Any) -> dict[str, Any]:
    found = problems(db, settings, spec)
    if found:
        raise ApiError(422, "invalid_spec", "The dashboard is not valid.", found)
    checked: dict[str, Any] = spec
    return checked


def create(
    db: Session, settings: Settings, actor: Principal, *, title: str, spec: Any | None = None
) -> Dashboard:
    content = _checked(db, settings, spec) if spec is not None else specs.blank_spec(title.strip())
    dashboard = Dashboard(title=content["title"], owner_id=actor.user_id, version=0)
    db.add(dashboard)
    db.flush()
    _write_version(db, dashboard, content, "user", actor.user_id, "Created")
    audit.record(
        db,
        action="dashboard.created",
        actor=actor,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title},
    )
    db.commit()
    return dashboard


def save(
    db: Session,
    settings: Settings,
    actor: Principal,
    dashboard_id: uuid.UUID,
    spec: Any,
    *,
    base_version: int | None = None,
    message: str | None = None,
    author_type: str = "user",
) -> Dashboard:
    """Validate and store a spec as a new version (the new draft).

    A stale `base_version` is refused with 409, so two editors, or a person
    and the agent, never silently overwrite each other.
    """
    dashboard = db.scalar(select(Dashboard).where(Dashboard.id == dashboard_id).with_for_update())
    if dashboard is None:
        raise ApiError(404, "not_found", "Dashboard not found.")
    if base_version is not None and base_version != dashboard.version:
        raise ApiError(
            409,
            "version_conflict",
            f"The dashboard has changed since version {base_version} (it is now version "
            f"{dashboard.version}). Load the latest version and apply your change again.",
            [{"path": "/baseVersion", "message": f"Current version is {dashboard.version}."}],
        )
    _write_version(db, dashboard, _checked(db, settings, spec), author_type, actor.user_id, message)
    db.commit()
    return dashboard


def list_versions(db: Session, dashboard: Dashboard) -> list[dict[str, Any]]:
    rows = db.execute(
        select(DashboardVersion, User.display_name)
        .outerjoin(User, User.id == DashboardVersion.author_id)
        .where(DashboardVersion.dashboard_id == dashboard.id)
        .order_by(DashboardVersion.version.desc())
        .limit(200)
    ).all()
    return [
        {
            "version": v.version,
            "authorType": v.author_type,
            "authorName": name
            or {"system": "System", "agent": "Assistant"}.get(v.author_type, "Deleted user"),
            "message": v.message,
            "createdAt": v.created_at.isoformat(),
            "current": v.version == dashboard.version,
            "published": v.version == dashboard.published_version,
        }
        for v, name in rows
    ]


def get_version(db: Session, dashboard: Dashboard, number: int) -> dict[str, Any]:
    return {"version": number, "spec": _version_row(db, dashboard, number).spec}


def restore(
    db: Session, settings: Settings, actor: Principal, dashboard: Dashboard, number: int
) -> Dashboard:
    old = _version_row(db, dashboard, number).spec
    return save(db, settings, actor, dashboard.id, old, message=f"Restored version {number}")


def publish(db: Session, actor: Principal, dashboard: Dashboard, number: int | None) -> Dashboard:
    target = number if number is not None else dashboard.version
    _version_row(db, dashboard, target)
    dashboard.published_version = target
    audit.record(
        db,
        action="dashboard.published",
        actor=actor,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title, "version": target},
    )
    db.commit()
    return dashboard


def unpublish(db: Session, actor: Principal, dashboard: Dashboard) -> Dashboard:
    dashboard.published_version = None
    audit.record(
        db,
        action="dashboard.unpublished",
        actor=actor,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title},
    )
    db.commit()
    return dashboard


def delete(db: Session, actor: Principal, dashboard: Dashboard) -> None:
    audit.record(
        db,
        action="dashboard.deleted",
        actor=actor,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title},
    )
    db.delete(dashboard)
    db.commit()


# ---- data -------------------------------------------------------------------------


def _resolve_spec(
    db: Session,
    settings: Settings,
    principal: Principal,
    dashboard: Dashboard,
    source: str,
    inline: Any | None,
) -> dict[str, Any]:
    """Which spec a data request runs against.

    "inline" lets the editor preview unsaved changes: only editors may send one,
    and it is checked for structure (the compiler reports anything else).
    """
    if source != "inline":
        return spec_for(db, dashboard, principal, source)
    if not can_edit(principal):
        raise ApiError(403, "forbidden", "Only editors can preview unsaved changes.")
    found = schema_problems(schema_validator(settings.spec_dir, "dashboard.schema.json"), inline)
    if found or not isinstance(inline, dict):
        raise ApiError(422, "invalid_spec", "The dashboard is not valid.", found)
    return inline


def item_data(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    principal: Principal,
    dashboard: Dashboard,
    *,
    item_id: str,
    source: str,
    inline_spec: Any | None,
    state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Rows for one dashboard item, derived from its bindings and the filter state."""
    spec = _resolve_spec(db, settings, principal, dashboard, source, inline_spec)
    item = next((i for i in spec["items"] if i["id"] == item_id), None)
    if item is None:
        raise ApiError(404, "not_found", f"The dashboard has no item with the id {item_id!r}.")
    manifest = manifests(settings).get(item["visual"])
    if manifest is None:
        raise ApiError(422, "invalid_spec", f"There is no visual called {item['visual']!r}.")

    names = {item["dataset"]} if item.get("dataset") else set()
    for control in spec.get("filters") or []:
        names.add(control["field"].partition(".")[0])
    for entries in (item.get("bindings") or {}).values():
        for entry in entries:
            ref = entry if isinstance(entry, str) else entry["field"]
            names.add(ref.partition(".")[0])
    for cross in (state or {}).get("cross") or []:
        if isinstance(cross, dict) and isinstance(cross.get("field"), str):
            names.add(cross["field"].partition(".")[0])
    sources = queries.load_sources(db, settings, names)
    # Filters and cross-filters may name a field of a dataset this item joins to.
    base = sources.get(item.get("dataset") or "")
    joined = {join.to for join in base.model.joins} - set(sources) if base else set()
    if joined:
        sources.update(queries.load_sources(db, settings, joined))
    models = {name: source_.model for name, source_ in sources.items()}
    db.rollback()

    query = specs.item_query(spec, item, manifest, models, state)
    if query is None:
        return {"itemId": item_id, "empty": True}
    if query["dataset"] not in sources:
        raise ApiError(
            409, "no_data", f"Dataset {query['dataset']!r} has no data yet. Refresh it first."
        )
    queries.check_query(settings, query)
    user = queries.user_context(principal, None)
    _, body = queries.execute(settings, runtime, sources, user, query)
    body["itemId"] = item_id

    mode = (item.get("config") or {}).get("comparison")
    if mode and mode != "none":
        compared = specs.comparison_query(query, mode, settings.calendar, settings.today())
        if compared is not None:
            _, previous = queries.execute(settings, runtime, sources, user, compared[0])
            body["comparison"] = {"mode": mode, "range": compared[1], "rows": previous["rows"]}
    if can_edit(principal):
        body["query"] = query
    return body


def filter_options(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    principal: Principal,
    dashboard: Dashboard,
    *,
    filter_id: str,
    source: str,
    inline_spec: Any | None,
    search: str | None,
) -> dict[str, Any]:
    """Distinct values for a select or multiSelect filter, with RLS applied."""
    spec = _resolve_spec(db, settings, principal, dashboard, source, inline_spec)
    control = next((f for f in spec.get("filters") or [] if f["id"] == filter_id), None)
    if control is None:
        raise ApiError(404, "not_found", f"The dashboard has no filter with the id {filter_id!r}.")
    field = control["field"]
    dataset = field.partition(".")[0]
    query: dict[str, Any] = {
        "dataset": dataset,
        "dimensions": [field],
        "sort": [{"field": field, "dir": "asc"}],
        "limit": MAX_FILTER_OPTIONS,
    }
    if search:
        query["filters"] = [{"field": field, "op": "contains", "values": [search[:200]]}]
    sources = queries.load_sources(db, settings, {dataset})
    db.rollback()
    if dataset not in sources:
        return {"values": [], "truncated": False}
    queries.check_query(settings, query)
    _, body = queries.execute(
        settings, runtime, sources, queries.user_context(principal, None), query
    )
    values = [row[0] for row in body["rows"] if row[0] is not None]
    return {"values": values, "truncated": body["truncated"]}
