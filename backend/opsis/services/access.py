"""Who may do what with a dashboard or a dataset (brief section 11).

Dashboards: admins can do everything. Otherwise a dashboard belongs to its
owner and is shared with users, groups or everyone, as a viewer or an editor.
Viewers see the published version only; editors see and change the draft, but
only when their workspace role allows editing at all. A viewer-role person
shared as an editor is still a viewer.

Datasets: anyone who can build may use a dataset unless it is restricted; a
restricted dataset can be used by admins and its grantees only. Viewing a
dashboard needs no dataset access: the dashboard grants its own questions,
with row-level security applied as the viewer.

Groups are managed here too, because they exist for sharing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Permission, Principal
from opsis.db.models import (
    Dashboard,
    DashboardShare,
    Dataset,
    DatasetGrant,
    Group,
    GroupMember,
    User,
)
from opsis.errors import ApiError
from opsis.services import audit

LEVELS = {"viewer": 1, "editor": 2}


@dataclass(frozen=True)
class DashboardAccess:
    view: bool
    edit: bool
    publish: bool
    share: bool
    # How the principal reached the dashboard: admin, owner, editor, viewer.
    via: str | None


def dashboard_access(db: Session, dashboard: Dashboard, principal: Principal) -> DashboardAccess:
    published = dashboard.published_version is not None
    can_build = principal.can(Permission.DASHBOARDS_EDIT)
    can_publish = principal.can(Permission.DASHBOARDS_PUBLISH)
    if principal.is_admin:
        return DashboardAccess(True, True, True, True, "admin")
    via: str | None
    if dashboard.owner_id == principal.user_id and principal.auth_method != "share_link":
        level = 2
        via = "owner"
    else:
        roles = db.scalars(
            select(DashboardShare.role).where(
                DashboardShare.dashboard_id == dashboard.id,
                DashboardShare.grantee.in_(principal.grantees),
            )
        ).all()
        level = max((LEVELS.get(r, 0) for r in roles), default=0)
        via = {2: "editor", 1: "viewer"}.get(level)
    edit = level >= 2 and can_build
    return DashboardAccess(
        view=edit or (level >= 1 and published),
        edit=edit,
        publish=edit and can_publish,
        share=edit,
        via=via,
    )


def require_dashboard(
    db: Session, dashboard: Dashboard, principal: Principal, need: str
) -> DashboardAccess:
    """Raise unless the principal may `need` ("view", "edit", "publish" or "share").

    A dashboard someone cannot even view answers 404, so its existence is not revealed."""
    access = dashboard_access(db, dashboard, principal)
    if not access.view:
        raise ApiError(404, "not_found", "Dashboard not found.")
    if not getattr(access, need):
        message = {
            "edit": "You can view this dashboard but not change it. Ask its owner to share "
            "it with you as an editor.",
            "publish": "You cannot publish this dashboard.",
            "share": "You cannot change who this dashboard is shared with.",
        }.get(need, "You do not have access to this dashboard.")
        raise ApiError(403, "forbidden", message)
    return access


def visible_dashboards(
    db: Session, principal: Principal
) -> dict[uuid.UUID, DashboardAccess] | None:
    """Every dashboard the principal can see, with their access. None means all (admin)."""
    if principal.is_admin:
        return None
    shared = dict(
        db.execute(
            select(DashboardShare.dashboard_id, func.max(DashboardShare.role))
            .where(DashboardShare.grantee.in_(principal.grantees))
            .group_by(DashboardShare.dashboard_id)
        ).all()
    )
    # "viewer" < "editor" alphabetically too, so max() above picks the strongest role.
    candidates = db.scalars(
        select(Dashboard).where(
            or_(Dashboard.owner_id == principal.user_id, Dashboard.id.in_(list(shared)))
        )
    ).all()
    result = {}
    for dashboard in candidates:
        access = dashboard_access(db, dashboard, principal)
        if access.view:
            result[dashboard.id] = access
    return result


# ---- sharing a dashboard ------------------------------------------------------------


def _grantee_names(db: Session, grantees: list[str]) -> dict[str, dict[str, Any]]:
    user_ids: list[uuid.UUID] = []
    group_ids: list[uuid.UUID] = []
    for grantee in grantees:
        kind, _, value = grantee.partition(":")
        try:
            target = uuid.UUID(value)
        except ValueError:
            continue
        if kind == "user":
            user_ids.append(target)
        elif kind == "group":
            group_ids.append(target)
    names: dict[str, dict[str, Any]] = {"everyone": {"name": "Everyone", "kind": "everyone"}}
    for user in db.scalars(select(User).where(User.id.in_(user_ids))).all():
        names[f"user:{user.id}"] = {
            "name": user.display_name,
            "email": user.email,
            "kind": "user",
            "role": user.role,
        }
    for group in db.scalars(select(Group).where(Group.id.in_(group_ids))).all():
        names[f"group:{group.id}"] = {"name": group.name, "kind": "group"}
    return names


def check_grantee(db: Session, grantee: str, path: str) -> str:
    kind, _, value = grantee.partition(":")
    if grantee == "everyone":
        return grantee
    try:
        target = uuid.UUID(value)
    except ValueError:
        target = None
    exists = (
        kind == "user"
        and target
        and db.scalar(select(User.id).where(User.id == target, User.is_builtin.is_(False)))
    ) or (kind == "group" and target and db.get(Group, target))
    if not exists:
        raise ApiError(
            422,
            "invalid_grantee",
            f"{grantee!r} is not a user or group. Write user:<id>, group:<id> or everyone.",
            [{"path": path, "message": "Unknown user or group."}],
        )
    return f"{kind}:{target}"


def dashboard_shares(db: Session, dashboard: Dashboard) -> dict[str, Any]:
    rows = db.scalars(
        select(DashboardShare)
        .where(DashboardShare.dashboard_id == dashboard.id)
        .order_by(DashboardShare.created_at)
    ).all()
    names = _grantee_names(db, [r.grantee for r in rows])
    owner = db.get(User, dashboard.owner_id) if dashboard.owner_id else None
    return {
        "owner": {"id": str(owner.id), "name": owner.display_name, "email": owner.email}
        if owner
        else None,
        "shares": [
            {
                "grantee": r.grantee,
                "role": r.role,
                **names.get(r.grantee, {"name": "Deleted", "kind": "missing"}),
            }
            for r in rows
            if r.grantee in names
        ],
    }


def set_dashboard_shares(
    db: Session, actor: Principal, dashboard: Dashboard, shares: list[dict[str, str]]
) -> dict[str, Any]:
    """Replace who a dashboard is shared with."""
    require_dashboard(db, dashboard, actor, "share")
    wanted: dict[str, str] = {}
    for index, share in enumerate(shares):
        role = share.get("role")
        if role not in LEVELS:
            raise ApiError(
                422,
                "invalid_request",
                "A share's role must be viewer or editor.",
                [{"path": f"/shares/{index}/role", "message": "viewer or editor"}],
            )
        wanted[check_grantee(db, str(share.get("grantee", "")), f"/shares/{index}/grantee")] = role
    before = {
        r.grantee: r.role
        for r in db.scalars(
            select(DashboardShare).where(DashboardShare.dashboard_id == dashboard.id)
        )
    }
    db.execute(delete(DashboardShare).where(DashboardShare.dashboard_id == dashboard.id))
    for grantee, role in wanted.items():
        db.add(
            DashboardShare(
                dashboard_id=dashboard.id, grantee=grantee, role=role, created_by=actor.user_id
            )
        )
    if before != wanted:
        audit.record(
            db,
            action="dashboard.shared",
            actor=actor,
            target_type="dashboard",
            target_id=str(dashboard.id),
            detail={"title": dashboard.title, "before": before, "after": wanted},
        )
    db.commit()
    return dashboard_shares(db, dashboard)


def share_with(
    db: Session, actor: Principal, dashboard: Dashboard, grantee: str, role: str
) -> dict[str, Any]:
    """Add or change one share, keeping the others (used by the agent)."""
    current = [
        {"grantee": s["grantee"], "role": s["role"]}
        for s in dashboard_shares(db, dashboard)["shares"]
    ]
    current = [s for s in current if s["grantee"] != grantee]
    if role != "none":
        current.append({"grantee": grantee, "role": role})
    return set_dashboard_shares(db, actor, dashboard, current)


def find_grantee(db: Session, text: str) -> str:
    """Resolve "everyone", an email, or a group name to a grantee (for the agent)."""
    value = text.strip()
    if value.lower() in ("everyone", "all", "workspace"):
        return "everyone"
    if "@" in value:
        user = db.scalar(
            select(User).where(User.email == value.lower(), User.is_builtin.is_(False))
        )
        if user is not None:
            return f"user:{user.id}"
    group = db.scalar(select(Group).where(func.lower(Group.name) == value.lower()))
    if group is not None:
        return f"group:{group.id}"
    users = [
        u.email
        for u in db.scalars(select(User).where(User.is_active, User.is_builtin.is_(False))).all()
    ]
    groups = [g.name for g in db.scalars(select(Group)).all()]
    raise ApiError(
        404,
        "not_found",
        f"There is no user or group called {value!r}. Use an email "
        f"({', '.join(users[:10]) or 'none yet'}), "
        f"a group name ({', '.join(groups[:10]) or 'none yet'}), or everyone.",
    )


def directory(db: Session) -> dict[str, Any]:
    """People and groups to share with."""
    users = db.scalars(
        select(User)
        .where(User.is_active, User.is_builtin.is_(False))
        .order_by(func.lower(User.display_name))
    ).all()
    counts = dict(
        db.execute(select(GroupMember.group_id, func.count()).group_by(GroupMember.group_id)).all()
    )
    groups = db.scalars(select(Group).order_by(func.lower(Group.name))).all()
    return {
        "users": [
            {"id": str(u.id), "name": u.display_name, "email": u.email, "role": u.role}
            for u in users
        ],
        "groups": [
            {"id": str(g.id), "name": g.name, "members": counts.get(g.id, 0)} for g in groups
        ],
    }


# ---- datasets -----------------------------------------------------------------------


def blocked_datasets(db: Session, principal: Principal) -> set[str]:
    """Names of datasets this principal may not use (restricted, not granted)."""
    if principal.is_admin:
        return set()
    restricted = db.execute(select(Dataset.id, Dataset.name).where(Dataset.restricted)).all()
    if not restricted:
        return set()
    granted = set(
        db.scalars(
            select(DatasetGrant.dataset_id).where(DatasetGrant.grantee.in_(principal.grantees))
        ).all()
    )
    return {name for dataset_id, name in restricted if dataset_id not in granted}


def require_datasets(db: Session, principal: Principal, names: set[str]) -> None:
    blocked = sorted(names & blocked_datasets(db, principal))
    if blocked:
        listed = ", ".join(repr(n) for n in blocked)
        raise ApiError(
            403,
            "dataset_forbidden",
            f"You do not have access to the dataset{'s' if len(blocked) > 1 else ''} {listed}. "
            "Ask an admin to give you access.",
        )


def dataset_grants(db: Session, dataset: Dataset) -> dict[str, Any]:
    rows = db.scalars(select(DatasetGrant).where(DatasetGrant.dataset_id == dataset.id)).all()
    names = _grantee_names(db, [r.grantee for r in rows])
    return {
        "restricted": dataset.restricted,
        "grants": [{"grantee": r.grantee, **names[r.grantee]} for r in rows if r.grantee in names],
    }


def set_dataset_grants(
    db: Session, actor: Principal, dataset: Dataset, restricted: bool, grantees: list[str]
) -> dict[str, Any]:
    checked = [check_grantee(db, g, f"/grants/{i}") for i, g in enumerate(grantees)]
    dataset.restricted = restricted
    db.execute(delete(DatasetGrant).where(DatasetGrant.dataset_id == dataset.id))
    for grantee in dict.fromkeys(checked):
        if grantee != "everyone":
            db.add(DatasetGrant(dataset_id=dataset.id, grantee=grantee, created_by=actor.user_id))
    audit.record(
        db,
        action="dataset.access_changed",
        actor=actor,
        target_type="dataset",
        target_id=str(dataset.id),
        detail={"dataset": dataset.name, "restricted": restricted, "grants": checked},
    )
    db.commit()
    return dataset_grants(db, dataset)


# ---- groups -------------------------------------------------------------------------


def list_groups(db: Session) -> list[dict[str, Any]]:
    members: dict[uuid.UUID, list[dict[str, str]]] = {}
    for group_id, user in db.execute(
        select(GroupMember.group_id, User)
        .join(User, User.id == GroupMember.user_id)
        .order_by(User.display_name)
    ).all():
        members.setdefault(group_id, []).append(
            {"id": str(user.id), "name": user.display_name, "email": user.email}
        )
    return [
        {
            "id": str(g.id),
            "name": g.name,
            "description": g.description,
            "members": members.get(g.id, []),
        }
        for g in db.scalars(select(Group).order_by(func.lower(Group.name))).all()
    ]


def save_group(
    db: Session,
    actor: Principal,
    group_id: uuid.UUID | None,
    *,
    name: str,
    description: str | None,
    member_ids: list[uuid.UUID] | None,
) -> dict[str, Any]:
    clean = name.strip()[:100]
    if not clean:
        raise ApiError(
            422,
            "invalid_request",
            "Give the group a name.",
            [{"path": "/name", "message": "Required."}],
        )
    clash = db.scalar(select(Group.id).where(func.lower(Group.name) == clean.lower()))
    if clash is not None and clash != group_id:
        raise ApiError(409, "group_exists", f"There is already a group called {clean!r}.")
    group = db.get(Group, group_id) if group_id else Group()
    if group is None:
        raise ApiError(404, "not_found", "Group not found.")
    group.name = clean
    group.description = (description or "").strip()[:500] or None
    db.add(group)
    db.flush()
    if member_ids is not None:
        known = set(
            db.scalars(
                select(User.id).where(User.id.in_(member_ids), User.is_builtin.is_(False))
            ).all()
        )
        db.execute(delete(GroupMember).where(GroupMember.group_id == group.id))
        for user_id in dict.fromkeys(member_ids):
            if user_id in known:
                db.add(GroupMember(group_id=group.id, user_id=user_id))
    audit.record(
        db,
        action="group.saved",
        actor=actor,
        target_type="group",
        target_id=str(group.id),
        detail={"name": clean, "members": len(member_ids) if member_ids is not None else None},
    )
    db.commit()
    return next(g for g in list_groups(db) if g["id"] == str(group.id))


def delete_group(db: Session, actor: Principal, group_id: uuid.UUID) -> None:
    group = db.get(Group, group_id)
    if group is None:
        raise ApiError(404, "not_found", "Group not found.")
    grantee = f"group:{group.id}"
    db.execute(delete(DashboardShare).where(DashboardShare.grantee == grantee))
    db.execute(delete(DatasetGrant).where(DatasetGrant.grantee == grantee))
    audit.record(
        db,
        action="group.deleted",
        actor=actor,
        target_type="group",
        target_id=str(group.id),
        detail={"name": group.name},
    )
    db.delete(group)
    db.commit()
