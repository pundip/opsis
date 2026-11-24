"""Dashboards: versions, draft and published states, and the data behind each item.

Every function that reads or changes a dashboard checks access here
(services/access.py), so the REST API, the MCP tools and share links all
follow the same rules.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.dashboards import patch as patches
from opsis.dashboards import spec as specs
from opsis.dashboards.manifests import Manifest, load_manifests
from opsis.db.models import Dashboard, DashboardVersion, Dataset, User
from opsis.errors import ApiError
from opsis.semantic.compiler import UserContext
from opsis.semantic.model import Model, Problem, schema_problems, schema_validator
from opsis.services import access as acl
from opsis.services import audit, events, schedules
from opsis.services import query as queries
from opsis.services.query import QueryRuntime
from opsis.settings import Settings

MAX_FILTER_OPTIONS = 500


def manifests(settings: Settings) -> dict[str, Manifest]:
    return load_manifests(settings.visuals_dir, settings.spec_dir)


def all_models(
    db: Session, settings: Settings, principal: Principal | None = None
) -> dict[str, Model]:
    """The semantic model of every dataset that has data, and that `principal`
    (when given) may use."""
    names = set(db.scalars(select(Dataset.name)).all())
    if principal is not None:
        names -= acl.blocked_datasets(db, principal)
    return {
        name: source.model for name, source in queries.load_sources(db, settings, names).items()
    }


def problems(
    db: Session, settings: Settings, spec: Any, principal: Principal | None = None
) -> list[Problem]:
    """Everything wrong with a spec. With a principal, a dataset they may not
    use counts as missing, so nobody can build on data they cannot query."""
    return specs.validate(
        settings.spec_dir, spec, manifests(settings), all_models(db, settings, principal)
    )


spec_datasets = specs.spec_datasets  # kept here for callers of this module


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


def published_spec(db: Session, dashboard: Dashboard) -> dict[str, Any]:
    if dashboard.published_version is None:
        raise ApiError(404, "not_published", "This dashboard has not been published yet.")
    return _version_row(db, dashboard, dashboard.published_version).spec


def spec_for(
    db: Session, dashboard: Dashboard, principal: Principal, source: str
) -> dict[str, Any]:
    """The spec a caller may see. Only those who may edit can read the draft."""
    access = acl.require_dashboard(db, dashboard, principal, "view")
    if source == "draft" and access.edit:
        return _version_row(db, dashboard, dashboard.version).spec
    return published_spec(db, dashboard)


def summary(
    dashboard: Dashboard,
    owner: str | None = None,
    access: acl.DashboardAccess | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    result = {
        "id": str(dashboard.id),
        "title": title or dashboard.title,
        "version": dashboard.version,
        "publishedVersion": dashboard.published_version,
        "hasUnpublishedChanges": dashboard.published_version != dashboard.version,
        "ownerName": owner,
        "createdAt": dashboard.created_at.isoformat(),
        "updatedAt": dashboard.updated_at.isoformat(),
    }
    if access is not None:
        result["canEdit"] = access.edit
        result["access"] = access.via
        if not access.edit:
            # Viewers only know the published version; the draft is none of their business.
            result["hasUnpublishedChanges"] = False
    return result


def published_titles(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The title of the published version of each dashboard (dashboards.title follows the draft)."""
    if not ids:
        return {}
    rows = db.execute(
        select(DashboardVersion.dashboard_id, DashboardVersion.spec["title"].astext)
        .join(Dashboard, Dashboard.id == DashboardVersion.dashboard_id)
        .where(
            DashboardVersion.dashboard_id.in_(ids),
            DashboardVersion.version == Dashboard.published_version,
        )
    ).all()
    return {dashboard_id: title for dashboard_id, title in rows if title}


def list_dashboards(db: Session, principal: Principal) -> list[dict[str, Any]]:
    visible = acl.visible_dashboards(db, principal)
    query = (
        select(Dashboard, User.display_name)
        .outerjoin(User, User.id == Dashboard.owner_id)
        .order_by(func.lower(Dashboard.title))
    )
    if visible is not None:
        query = query.where(Dashboard.id.in_(list(visible)))
    rows = db.execute(query).all()
    full = acl.DashboardAccess(True, True, True, True, "admin")
    titles = published_titles(
        db, [d.id for d, _ in rows if visible is not None and not visible[d.id].edit]
    )
    result = [
        summary(d, owner, full if visible is None else visible[d.id], titles.get(d.id))
        for d, owner in rows
    ]
    return sorted(result, key=lambda d: str(d["title"]).lower())


def detail(db: Session, dashboard: Dashboard, principal: Principal) -> dict[str, Any]:
    """The dashboard as this caller sees it: editors get the draft, others the published version."""
    access = acl.require_dashboard(db, dashboard, principal, "view")
    source = "draft" if access.edit else "published"
    owner = db.scalar(select(User.display_name).where(User.id == dashboard.owner_id))
    spec = spec_for(db, dashboard, principal, source)
    return {
        **summary(dashboard, owner, access, spec.get("title")),
        "source": source,
        "spec": spec,
        "canPublish": access.publish,
        "canShare": access.share,
        "isOwner": access.via == "owner",
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


def _announce(
    dashboard: Dashboard,
    event: str,
    actor: Principal,
    author_type: str,
    *,
    message: str | None = None,
    client_id: str | None = None,
) -> None:
    """Tell open editors that the dashboard changed. Call after the commit."""
    events.hub.publish(
        events.dashboard_channel(dashboard.id),
        event,
        {
            "dashboardId": str(dashboard.id),
            "version": dashboard.version,
            "publishedVersion": dashboard.published_version,
            "authorType": author_type,
            "authorName": actor.display_name,
            "message": message,
            # Echoed so the editor that made the change can ignore its own event.
            "clientId": client_id,
        },
    )


def _checked(db: Session, settings: Settings, spec: Any, actor: Principal) -> dict[str, Any]:
    found = problems(db, settings, spec, actor)
    if found:
        raise ApiError(422, "invalid_spec", "The dashboard is not valid.", found)
    checked: dict[str, Any] = spec
    return checked


def create(
    db: Session,
    settings: Settings,
    actor: Principal,
    *,
    title: str,
    spec: Any | None = None,
    author_type: str = "user",
    message: str | None = None,
) -> Dashboard:
    content = (
        _checked(db, settings, spec, actor) if spec is not None else specs.blank_spec(title.strip())
    )
    dashboard = Dashboard(title=content["title"], owner_id=actor.user_id, version=0)
    db.add(dashboard)
    db.flush()
    _write_version(db, dashboard, content, author_type, actor.user_id, message or "Created")
    audit.record(
        db,
        action="dashboard.created",
        actor=actor,
        actor_type=author_type,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title},
    )
    db.commit()
    return dashboard


def _conflict(base_version: int, current: int) -> ApiError:
    return ApiError(
        409,
        "version_conflict",
        f"The dashboard has changed since version {base_version} (it is now version "
        f"{current}). Load the latest version and apply your change again.",
        [{"path": "/baseVersion", "message": f"Current version is {current}."}],
    )


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
    client_id: str | None = None,
) -> Dashboard:
    """Validate and store a spec as a new version (the new draft).

    A stale `base_version` is refused with 409, so two editors, or a person
    and the agent, never silently overwrite each other.
    """
    dashboard = db.scalar(select(Dashboard).where(Dashboard.id == dashboard_id).with_for_update())
    if dashboard is None:
        raise ApiError(404, "not_found", "Dashboard not found.")
    acl.require_dashboard(db, dashboard, actor, "edit")
    if base_version is not None and base_version != dashboard.version:
        raise _conflict(base_version, dashboard.version)
    checked = _checked(db, settings, spec, actor)
    acl.check_draft(db, actor, dashboard, specs.spec_datasets(checked))
    _write_version(db, dashboard, checked, author_type, actor.user_id, message)
    db.commit()
    _announce(
        dashboard, "version_created", actor, author_type, message=message, client_id=client_id
    )
    return dashboard


def patch(
    db: Session,
    settings: Settings,
    actor: Principal,
    dashboard_id: uuid.UUID,
    ops: Any,
    *,
    base_version: int,
    message: str | None = None,
    author_type: str = "agent",
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Dashboard:
    """Apply JSON Patch operations to the draft and store the result as a new version.

    The patch is applied to `base_version`, which must be the latest version:
    a stale one is refused with 409 before anything is applied. `transform` gets
    the patched spec before it is checked (the agent tools forgive slips there).
    """
    dashboard = get(db, dashboard_id)
    acl.require_dashboard(db, dashboard, actor, "edit")
    if base_version != dashboard.version:
        raise _conflict(base_version, dashboard.version)
    current = _version_row(db, dashboard, dashboard.version).spec
    patched = patches.apply(current, ops)
    if transform is not None:
        patched = transform(patched)
    return save(
        db,
        settings,
        actor,
        dashboard_id,
        patched,
        base_version=base_version,
        message=message,
        author_type=author_type,
    )


def list_versions(db: Session, dashboard: Dashboard, principal: Principal) -> list[dict[str, Any]]:
    acl.require_dashboard(db, dashboard, principal, "edit")
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


def get_version(
    db: Session, dashboard: Dashboard, number: int, principal: Principal
) -> dict[str, Any]:
    acl.require_dashboard(db, dashboard, principal, "edit")
    return {"version": number, "spec": _version_row(db, dashboard, number).spec}


def restore(
    db: Session,
    settings: Settings,
    actor: Principal,
    dashboard: Dashboard,
    number: int,
    *,
    author_type: str = "user",
    client_id: str | None = None,
) -> Dashboard:
    acl.require_dashboard(db, dashboard, actor, "edit")
    old = _version_row(db, dashboard, number).spec
    return save(
        db,
        settings,
        actor,
        dashboard.id,
        old,
        message=f"Restored version {number}",
        author_type=author_type,
        client_id=client_id,
    )


def publish(
    db: Session,
    actor: Principal,
    dashboard: Dashboard,
    number: int | None,
    *,
    author_type: str = "user",
    client_id: str | None = None,
) -> Dashboard:
    acl.require_dashboard(db, dashboard, actor, "publish")
    target = number if number is not None else dashboard.version
    _version_row(db, dashboard, target)
    acl.check_publish(db, actor, dashboard, target)
    dashboard.published_version = target
    audit.record(
        db,
        action="dashboard.published",
        actor=actor,
        actor_type=author_type,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title, "version": target},
    )
    db.commit()
    _announce(dashboard, "published", actor, author_type, client_id=client_id)
    return dashboard


def unpublish(db: Session, actor: Principal, dashboard: Dashboard) -> Dashboard:
    acl.require_dashboard(db, dashboard, actor, "publish")
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
    access = acl.require_dashboard(db, dashboard, actor, "edit")
    if access.via not in ("admin", "owner"):
        raise ApiError(403, "forbidden", "Only the owner or an admin can delete this dashboard.")
    audit.record(
        db,
        action="dashboard.deleted",
        actor=actor,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title},
    )
    schedules.forget(db, dashboard.id)  # its emailed snapshots
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
) -> tuple[dict[str, Any], acl.DashboardAccess]:
    """Which spec a data request runs against, and the caller's access.

    "inline" lets the editor preview unsaved changes: only those who may edit
    can send one, it is checked for structure (the compiler reports anything
    else), and it may only read datasets the caller may use.
    """
    access = acl.require_dashboard(db, dashboard, principal, "view")
    if source != "inline":
        return spec_for(db, dashboard, principal, source), access
    if not access.edit:
        raise ApiError(403, "forbidden", "Only editors can preview unsaved changes.")
    found = schema_problems(schema_validator(settings.spec_dir, "dashboard.schema.json"), inline)
    if found or not isinstance(inline, dict):
        raise ApiError(422, "invalid_spec", "The dashboard is not valid.", found)
    acl.require_datasets(db, principal, spec_datasets(inline))
    return inline, access


def viewer_context(
    db: Session, principal: Principal, view_as: Mapping[str, Any] | None
) -> UserContext:
    """Who row-level security sees: the caller, or (for an admin) "View as"."""
    return queries.user_context(principal, view_as, db=db)


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
    view_as: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rows for one dashboard item, derived from its bindings and the filter state."""
    spec, access = _resolve_spec(db, settings, principal, dashboard, source, inline_spec)
    user = viewer_context(db, principal, view_as)
    return data_for_spec(
        db, settings, runtime, spec, item_id, state, user=user, include_query=access.edit
    )


def data_for_spec(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    spec: Mapping[str, Any],
    item_id: str,
    state: Mapping[str, Any] | None,
    *,
    user: UserContext,
    include_query: bool,
) -> dict[str, Any]:
    """Rows for one item of an already authorised spec, as `user` may see them."""
    if state and state.get("cross"):
        # A dashboard answers its own questions only: a cross-filter must be on a
        # field the dashboard uses, not any field its datasets can reach.
        allowed = specs.spec_fields(spec)
        state = {
            **state,
            "cross": [
                c for c in state["cross"] if isinstance(c, Mapping) and c.get("field") in allowed
            ],
        }
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
    _, body = queries.execute(settings, runtime, sources, user, query)
    body["itemId"] = item_id

    mode = (item.get("config") or {}).get("comparison")
    if mode and mode != "none":
        compared = specs.comparison_query(query, mode, settings.calendar, settings.today())
        if compared is not None:
            _, previous = queries.execute(settings, runtime, sources, user, compared[0])
            body["comparison"] = {"mode": mode, "range": compared[1], "rows": previous["rows"]}
    if include_query:
        body["query"] = query
    else:
        # Which snapshot answered is internal; viewers and share links do not need it.
        body.pop("snapshots", None)
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
    view_as: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Distinct values for a select or multiSelect filter, with RLS applied."""
    spec, _ = _resolve_spec(db, settings, principal, dashboard, source, inline_spec)
    user = viewer_context(db, principal, view_as)
    return options_for_spec(db, settings, runtime, spec, filter_id, search, user=user)


def options_for_spec(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    spec: Mapping[str, Any],
    filter_id: str,
    search: str | None,
    *,
    user: UserContext,
) -> dict[str, Any]:
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
    _, body = queries.execute(settings, runtime, sources, user, query)
    values = [row[0] for row in body["rows"] if row[0] is not None]
    return {"values": values, "truncated": body["truncated"]}
