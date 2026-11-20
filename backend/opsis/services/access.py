"""Who may do what with a dashboard or a dataset (brief section 11).

Dashboards: admins can do everything. Otherwise a dashboard belongs to its
owner and is shared with users, groups or everyone, as a viewer or an editor.
Viewers see the published version only; editors see and change the draft, but
only when their workspace role allows editing at all. A viewer-role person
shared as an editor is still a viewer. Only the owner and admins change who a
dashboard is shared with, share links included (M6 review).

Datasets: anyone who can build may use a dataset unless it is restricted; a
restricted dataset can be used by admins, its owner (the person who created
it) and the people and groups on its list. Its owner and admins manage the
restriction; only an admin can lift a restriction an admin set (M6 review).

Restricted data through dashboards (M6 review): viewing a dashboard needs no
dataset access, because the dashboard grants its own questions. So a
dashboard that uses a restricted dataset may only be opened up to people
outside that dataset's list, or through a share link, by the dataset's owner
or an admin. That is checked when sharing changes, when a share link is made,
when such a dataset is added to a draft that editors outside the list can
see, and when it is first published.

Groups are managed here too, because they exist for sharing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from opsis.auth.principal import Permission, Principal, Role
from opsis.dashboards.spec import spec_datasets
from opsis.db.models import (
    Dashboard,
    DashboardShare,
    DashboardVersion,
    Dataset,
    DatasetGrant,
    Group,
    GroupMember,
    ShareLink,
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
        # Editors change content; only the owner (and admins) change who has access.
        share=edit and via == "owner",
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
            "share": "Only the dashboard's owner or an admin can change who it is shared "
            "with or make share links.",
        }.get(need, "You do not have access to this dashboard.")
        raise ApiError(403, "forbidden", message)
    return access


def visible_dashboards(
    db: Session, principal: Principal
) -> dict[uuid.UUID, DashboardAccess] | None:
    """Every dashboard the principal can see, with their access. None means all (admin)."""
    if principal.is_admin:
        return None
    shared = db.scalars(
        select(DashboardShare.dashboard_id)
        .where(DashboardShare.grantee.in_(principal.grantees))
        .distinct()
    ).all()
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


# ---- names of people and groups -------------------------------------------------------


def _grantee_names(db: Session, grantees: Iterable[str]) -> dict[str, dict[str, Any]]:
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


def _label(names: Mapping[str, Mapping[str, Any]], grantee: str) -> str:
    entry = names.get(grantee)
    if entry is None:
        return grantee
    if entry["kind"] == "group":
        return f"{entry['name']} (group)"
    return str(entry.get("email") or entry["name"])


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


# ---- restricted datasets ----------------------------------------------------------------


def dataset_owner_of(dataset: Dataset) -> uuid.UUID | None:
    """A dataset's owner is the person who created it."""
    return dataset.created_by


@dataclass
class Restriction:
    """Who may use one restricted dataset: admins, its owner, and its list."""

    dataset: Dataset
    owner: User | None
    inside_users: set[uuid.UUID] = field(default_factory=set)
    inside_groups: set[uuid.UUID] = field(default_factory=set)

    def admits(self, grantee: str) -> bool:
        """True when everyone a grantee stands for may already use the dataset.
        A group counts only when the group itself is on the list (its members change)."""
        kind, _, value = grantee.partition(":")
        try:
            target = uuid.UUID(value)
        except ValueError:
            return False
        if kind == "user":
            return target in self.inside_users
        if kind == "group":
            return target in self.inside_groups
        return False  # everyone

    def may_open_up(self, principal: Principal) -> bool:
        """Only the dataset's owner or an admin may show its data to people outside the list."""
        return principal.is_admin or (self.owner is not None and self.owner.id == principal.user_id)


def restrictions(db: Session, names: Iterable[str]) -> list[Restriction]:
    """The restricted datasets among `names`, with who is inside each."""
    rows = db.scalars(
        select(Dataset).where(Dataset.name.in_(list(set(names))), Dataset.restricted)
    ).all()
    if not rows:
        return []
    admins = set(
        db.scalars(
            select(User.id).where(User.role == Role.ADMIN.value, User.is_builtin.is_(False))
        ).all()
    )
    result = []
    for dataset in sorted(rows, key=lambda d: d.name):
        owner_id = dataset_owner_of(dataset)
        owner = db.get(User, owner_id) if owner_id else None
        restriction = Restriction(dataset, owner, inside_users=set(admins))
        if owner is not None:
            restriction.inside_users.add(owner.id)
        for grant in db.scalars(
            select(DatasetGrant.grantee).where(DatasetGrant.dataset_id == dataset.id)
        ).all():
            kind, _, value = grant.partition(":")
            try:
                target = uuid.UUID(value)
            except ValueError:
                continue
            if kind == "user":
                restriction.inside_users.add(target)
            elif kind == "group":
                restriction.inside_groups.add(target)
        if restriction.inside_groups:
            restriction.inside_users |= set(
                db.scalars(
                    select(GroupMember.user_id).where(
                        GroupMember.group_id.in_(list(restriction.inside_groups))
                    )
                ).all()
            )
        result.append(restriction)
    return result


def version_spec(db: Session, dashboard: Dashboard, version: int | None) -> Mapping[str, Any]:
    if version is None:
        return {}
    row = db.scalar(
        select(DashboardVersion.spec).where(
            DashboardVersion.dashboard_id == dashboard.id, DashboardVersion.version == version
        )
    )
    return row or {}


def dashboard_datasets(db: Session, dashboard: Dashboard) -> tuple[set[str], set[str]]:
    """Datasets read by the draft and by the published version."""
    draft = spec_datasets(version_spec(db, dashboard, dashboard.version))
    published = spec_datasets(version_spec(db, dashboard, dashboard.published_version))
    return draft, published


def active_links(db: Session, dashboard: Dashboard) -> int:
    now = datetime.now(UTC)
    return len(
        db.scalars(
            select(ShareLink.id).where(
                ShareLink.dashboard_id == dashboard.id,
                ShareLink.revoked_at.is_(None),
                or_(ShareLink.expires_at.is_(None), ShareLink.expires_at > now),
            )
        ).all()
    )


def _refuse_exposure(
    db: Session, problems: list[tuple[Restriction, list[str], bool]], doing: str
) -> None:
    """One message naming each restricted dataset, who would gain access, and who may do it."""
    if not problems:
        return
    names = _grantee_names(db, [g for _, grantees, _ in problems for g in grantees])
    parts, details = [], []
    for restriction, grantees, link in problems:
        who = [_label(names, g) for g in grantees] + (["anyone with a share link"] if link else [])
        owner = restriction.owner.display_name if restriction.owner else "nobody (deleted)"
        dataset = restriction.dataset
        parts.append(
            f"it uses the restricted dataset {dataset.name!r} (owner: {owner}), which "
            f"{', '.join(who)} may not use"
        )
        details.append(
            {
                "path": f"/datasets/{dataset.name}",
                "message": f"Restricted. Only {owner} (its owner) or an admin can {doing}.",
            }
        )
    raise ApiError(
        403,
        "restricted_data",
        f"This would show restricted data to people outside its list: {'; '.join(parts)}. "
        f"Only a dataset's owner or an admin can {doing}.",
        details,
    )


def check_new_audience(
    db: Session,
    actor: Principal,
    datasets: set[str],
    grantees: Iterable[str],
    *,
    link: bool = False,
    doing: str,
) -> None:
    """Refuse when `actor` would give `grantees` (or a share link) data from a
    restricted dataset among `datasets` without being its owner or an admin."""
    wanted = list(dict.fromkeys(grantees))
    problems = []
    for restriction in restrictions(db, datasets):
        if restriction.may_open_up(actor):
            continue
        outside = [g for g in wanted if not restriction.admits(g)]
        if outside or link:
            problems.append((restriction, outside, link))
    _refuse_exposure(db, problems, doing)


def dashboard_audience(db: Session, dashboard: Dashboard) -> dict[str, str]:
    """Who a dashboard is shared with ({grantee: role}), its owner counting as an editor."""
    shares = {
        r.grantee: r.role
        for r in db.scalars(
            select(DashboardShare).where(DashboardShare.dashboard_id == dashboard.id)
        )
    }
    if dashboard.owner_id is not None:
        shares[f"user:{dashboard.owner_id}"] = "editor"
    return shares


def check_draft(
    db: Session, actor: Principal, dashboard: Dashboard, new_datasets: set[str]
) -> None:
    """Before a draft adds a restricted dataset: everyone who can see drafts (the
    owner and editor shares) must be on its list, unless the actor may open it up."""
    draft, published = dashboard_datasets(db, dashboard)
    added = new_datasets - draft - published
    if not added:
        return
    editors = [g for g, role in dashboard_audience(db, dashboard).items() if role == "editor"]
    check_new_audience(
        db, actor, added, editors, doing="add it to a dashboard that people outside its list edit"
    )


def check_publish(db: Session, actor: Principal, dashboard: Dashboard, version: int) -> None:
    """Before a version is published: restricted datasets it adds to the published
    view must not reach viewers or share links outside their list."""
    _, published = dashboard_datasets(db, dashboard)
    added = spec_datasets(version_spec(db, dashboard, version)) - published
    if not added:
        return
    check_new_audience(
        db,
        actor,
        added,
        dashboard_audience(db, dashboard),
        link=active_links(db, dashboard) > 0,
        doing="publish it to people outside its list",
    )


def sharing_context(db: Session, dashboard: Dashboard, actor: Principal) -> list[dict[str, Any]]:
    """For the share dialog: the restricted datasets a dashboard uses, who is inside
    each, and whether the actor may open them up."""
    draft, published = dashboard_datasets(db, dashboard)
    return [
        {
            "dataset": r.dataset.name,
            "label": r.dataset.label,
            "owner": {"id": str(r.owner.id), "name": r.owner.display_name} if r.owner else None,
            "inDraft": r.dataset.name in draft,
            "inPublished": r.dataset.name in published,
            "canOpenUp": r.may_open_up(actor),
            "insideUserIds": sorted(str(u) for u in r.inside_users),
            "insideGroupIds": sorted(str(g) for g in r.inside_groups),
        }
        for r in restrictions(db, draft | published)
    ]


# ---- sharing a dashboard ------------------------------------------------------------


def dashboard_shares(
    db: Session, dashboard: Dashboard, actor: Principal | None = None
) -> dict[str, Any]:
    rows = db.scalars(
        select(DashboardShare)
        .where(DashboardShare.dashboard_id == dashboard.id)
        .order_by(DashboardShare.created_at)
    ).all()
    names = _grantee_names(db, [r.grantee for r in rows])
    owner = db.get(User, dashboard.owner_id) if dashboard.owner_id else None
    result: dict[str, Any] = {
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
    if actor is not None:
        result["restricted"] = sharing_context(db, dashboard, actor)
    return result


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
    # Restricted data: who newly sees the published version, and who newly sees the draft.
    draft, published = dashboard_datasets(db, dashboard)
    new_viewers = [g for g in wanted if g not in before]
    new_editors = [
        g for g, role in wanted.items() if role == "editor" and before.get(g) != "editor"
    ]
    check_new_audience(db, actor, published, new_viewers, doing="share it with them")
    check_new_audience(db, actor, draft, new_editors, doing="share it with them")

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
    return dashboard_shares(db, dashboard, actor)


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
    """Names of datasets this principal may not use (restricted, not granted, not theirs)."""
    if principal.is_admin:
        return set()
    restricted = db.execute(
        select(Dataset.id, Dataset.name, Dataset.created_by).where(Dataset.restricted)
    ).all()
    if not restricted:
        return set()
    granted = set(
        db.scalars(
            select(DatasetGrant.dataset_id).where(DatasetGrant.grantee.in_(principal.grantees))
        ).all()
    )
    return {
        name
        for dataset_id, name, owner in restricted
        if dataset_id not in granted and owner != principal.user_id
    }


def require_datasets(db: Session, principal: Principal, names: set[str]) -> None:
    blocked = sorted(names & blocked_datasets(db, principal))
    if blocked:
        listed = ", ".join(repr(n) for n in blocked)
        raise ApiError(
            403,
            "dataset_forbidden",
            f"You do not have access to the dataset{'s' if len(blocked) > 1 else ''} {listed}. "
            "Ask its owner or an admin to give you access.",
        )


def may_manage_dataset(principal: Principal, dataset: Dataset) -> bool:
    """The dataset's owner (with an editing role) and admins manage its restriction."""
    return principal.is_admin or (
        dataset_owner_of(dataset) == principal.user_id and principal.can(Permission.DATASETS_EDIT)
    )


def exposures(db: Session, dataset: Dataset) -> list[dict[str, Any]]:
    """Dashboards that show this (restricted) dataset's data to people outside its
    list, or through share links: made before the restriction, or by its owner."""
    if not dataset.restricted:
        return []
    restriction = restrictions(db, [dataset.name])[0]
    result = []
    for dashboard in db.scalars(select(Dashboard).order_by(Dashboard.title)).all():
        draft, published = dashboard_datasets(db, dashboard)
        if dataset.name not in draft | published:
            continue
        audience = dashboard_audience(db, dashboard)
        outside = [g for g in audience if not restriction.admits(g)]
        links = active_links(db, dashboard) if dataset.name in published else 0
        if outside or links:
            names = _grantee_names(db, outside)
            result.append(
                {
                    "id": str(dashboard.id),
                    "title": dashboard.title,
                    "outside": [_label(names, g) for g in outside],
                    "shareLinks": links,
                }
            )
    return result


def dataset_grants(db: Session, dataset: Dataset, actor: Principal | None = None) -> dict[str, Any]:
    rows = db.scalars(select(DatasetGrant).where(DatasetGrant.dataset_id == dataset.id)).all()
    names = _grantee_names(db, [r.grantee for r in rows])
    owner_id = dataset_owner_of(dataset)
    owner = db.get(User, owner_id) if owner_id else None
    restricted_by = db.get(User, dataset.restricted_by) if dataset.restricted_by else None
    result: dict[str, Any] = {
        "restricted": dataset.restricted,
        "grants": [{"grantee": r.grantee, **names[r.grantee]} for r in rows if r.grantee in names],
        "owner": {"id": str(owner.id), "name": owner.display_name} if owner else None,
        "restrictedBy": restricted_by.display_name if restricted_by else None,
        "restrictedByAdmin": dataset.restricted_by_admin,
        "exposures": exposures(db, dataset),
    }
    if actor is not None:
        result["canManage"] = may_manage_dataset(actor, dataset)
        result["canLift"] = actor.is_admin or not dataset.restricted_by_admin
    return result


def set_dataset_grants(
    db: Session, actor: Principal, dataset: Dataset, restricted: bool, grantees: list[str]
) -> dict[str, Any]:
    """Restrict a dataset, change its list, or lift the restriction.

    Its owner and admins may; an admin's restriction can only be lifted by an admin,
    and an admin saving a restriction makes it theirs."""
    if not may_manage_dataset(actor, dataset):
        raise ApiError(
            403,
            "forbidden",
            "Only the dataset's owner (the person who created it) or an admin can change "
            "who may use it.",
        )
    lifting = dataset.restricted and not restricted
    if lifting and dataset.restricted_by_admin and not actor.is_admin:
        raise ApiError(
            403,
            "forbidden",
            "An admin restricted this dataset, so only an admin can lift the "
            "restriction. You can still change who is on its list.",
        )
    checked = [check_grantee(db, g, f"/grants/{i}") for i, g in enumerate(grantees)]
    before = {"restricted": dataset.restricted, "byAdmin": dataset.restricted_by_admin}
    if restricted and (not dataset.restricted or actor.is_admin):
        dataset.restricted_by = actor.user_id
        dataset.restricted_by_admin = actor.is_admin
    elif not restricted:
        dataset.restricted_by = None
        dataset.restricted_by_admin = False
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
        detail={
            "dataset": dataset.name,
            "before": before,
            "restricted": restricted,
            "byAdmin": dataset.restricted_by_admin,
            "grants": checked,
        },
    )
    db.commit()
    return dataset_grants(db, dataset, actor)


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
