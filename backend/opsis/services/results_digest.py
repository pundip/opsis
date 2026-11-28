"""The results check (ADR-031, V2_DESIGN section 1.4): `python -m opsis
results-digest`. It reads, and changes nothing.

For every dashboard, draft and published, and every item that shows data, it runs the
item at its default filters as each viewer: an admin, every active person who may
open that version, and every live share link (published versions only, never exempt),
and keeps a hash of the rows.

`--compare FILE` runs again, counting relative dates from the day FILE was made, and
lists every item whose rows changed since, with who saw it. A digest made before the
upgrade to the workspace model also holds what its simulation of row-level security
through relationships predicted (`after`, rule R2 of ADR-032): each change says
whether it was predicted.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import Dashboard, DashboardVersion, ShareLink, User
from opsis.errors import ApiError
from opsis.semantic.compiler import UserContext
from opsis.services import access as acl
from opsis.services import dashboards, links, users
from opsis.services import query as queries
from opsis.services.query import QueryRuntime
from opsis.settings import Settings

FORMAT = "opsis-results-digest"
FORMAT_VERSION = 1
ADMIN = "admin"


@dataclass(frozen=True)
class Viewer:
    key: str  # admin, user:<id> or link:<id>
    label: str
    user: UserContext
    principal: Principal | None = None  # a person: which versions they may open
    link_dashboard: str | None = None  # a share link: its dashboard, published only


@dataclass(frozen=True)
class Version:
    which: str  # draft, published, or "draft and published" when they are the same
    number: int
    spec: Mapping[str, Any]


def runtime_for(settings: Settings) -> QueryRuntime:
    from opsis.query.duckdb_engine import DuckDBEngine, ResultCache

    return QueryRuntime(
        engine=DuckDBEngine(
            memory_limit_mb=settings.query_memory_limit_mb,
            threads=settings.query_threads,
            temp_dir=settings.data_dir / "tmp" / "duckdb",
        ),
        cache=ResultCache(settings.query_cache_entries),
        max_concurrent=1,
    )


def viewers(db: Session) -> list[Viewer]:
    found = [Viewer(ADMIN, "an admin (no attributes)", UserContext("admin"))]
    people = select(User).where(User.is_active.is_(True), User.is_builtin.is_(False))
    for user in db.scalars(people.order_by(User.display_name, User.email)):
        principal = users.principal_for(db, user, "results_digest")
        label = f"{user.display_name} <{user.email}> ({user.role})"
        found.append(
            Viewer(f"user:{user.id}", label, queries.user_context(principal, None), principal)
        )
    now = datetime.now(UTC)
    live = select(ShareLink).where(ShareLink.revoked_at.is_(None))
    for link in db.scalars(live.order_by(ShareLink.created_at, ShareLink.id)):
        if link.expires_at is not None and link.expires_at <= now:
            continue
        label = f"share link {link.name or link.prefix!r}"
        context = queries.user_context(links.principal_for(link), None)
        found.append(
            Viewer(f"link:{link.id}", label, context, link_dashboard=str(link.dashboard_id))
        )
    return found


def versions(db: Session, dashboard: Dashboard) -> list[Version]:
    def spec(number: int) -> Mapping[str, Any] | None:
        return db.scalar(
            select(DashboardVersion.spec).where(
                DashboardVersion.dashboard_id == dashboard.id, DashboardVersion.version == number
            )
        )

    found: list[Version] = []
    draft = spec(dashboard.version) if dashboard.version else None
    if dashboard.published_version == dashboard.version and draft is not None:
        return [Version("draft and published", dashboard.version, draft)]
    if draft is not None:
        found.append(Version("draft", dashboard.version, draft))
    if dashboard.published_version is not None:
        published = spec(dashboard.published_version)
        if published is not None:
            found.append(Version("published", dashboard.published_version, published))
    return found


def sees(db: Session, viewer: Viewer, dashboard: Dashboard, version: Version) -> bool:
    """Whether the viewer can open this version of the dashboard."""
    if viewer.key == ADMIN:
        return True
    if viewer.link_dashboard is not None:
        return viewer.link_dashboard == str(dashboard.id) and version.which != "draft"
    assert viewer.principal is not None
    access = acl.dashboard_access(db, dashboard, viewer.principal)
    if version.which == "draft":
        return access.edit
    if version.which == "published":
        return access.view
    return access.view or access.edit


def _plain(value: Any) -> Any:
    """Values as hashed: floats to 12 significant digits, so a sum added up in another
    order is still the same number."""
    if isinstance(value, float):
        return repr(value) if math.isnan(value) or math.isinf(value) else float(f"{value:.12g}")
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _rows(rows: Sequence[Any]) -> list[str]:
    return sorted(json.dumps(_plain(row), sort_keys=True, default=str) for row in rows)


def outcome(body: Mapping[str, Any]) -> dict[str, Any]:
    """What a digest keeps of one item's result: a hash of its rows (in any order),
    how many there are, and the total of each measure column."""
    if body.get("empty"):
        return {"empty": True}
    columns = [c["name"] for c in body["columns"]]
    material: dict[str, Any] = {"columns": columns, "rows": _rows(body["rows"])}
    if "comparison" in body:
        material["comparison"] = _rows(body["comparison"]["rows"])
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    totals: dict[str, Any] = {}
    for index, column in enumerate(body["columns"]):
        if column.get("role") != "measure":
            continue
        values = [row[index] for row in body["rows"] if isinstance(row[index], int | float)]
        if values and not any(isinstance(v, bool) for v in values):
            totals[column["name"]] = _plain(float(sum(values)))
    return {
        "hash": digest[:32],
        "rows": body["rowCount"],
        "truncated": bool(body.get("truncated")),
        "totals": totals,
    }


def _run(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return outcome(call())
    except ApiError as exc:
        return {"error": f"{exc.code}: {exc.message}"}


def same(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    if a is None or b is None:
        return a is b
    if "hash" in a or "hash" in b:
        return a.get("hash") == b.get("hash")
    return dict(a) == dict(b)


def digest(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    *,
    today: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Every item's result as each viewer sees it. Reads only. Row-level security
    travels through relationships (R2) since the workspace model: a digest made
    before it also holds what its simulation predicted (`after`), which `compare`
    checks."""
    today = today or settings.today()
    workspace = queries.load_workspace(db, settings)
    people = viewers(db)
    # What to run, worked out before anything runs (running rolls the session back).
    plan = []
    for dashboard in db.scalars(select(Dashboard).order_by(Dashboard.title, Dashboard.id)):
        for version in versions(db, dashboard):
            audience = [viewer for viewer in people if sees(db, viewer, dashboard, version)]
            plan.append((str(dashboard.id), dashboard.title, version, audience))
    db.rollback()

    entries = []
    boards = list(dict.fromkeys(board for board, _, _, _ in plan))
    for board, title, version, audience in plan:
        if progress:
            progress(
                f"Dashboard {boards.index(board) + 1} of {len(boards)}, {version.which}: {title}"
            )
        for item in version.spec.get("items") or []:
            if item.get("visual") == "text":
                continue
            for viewer in audience:
                call = partial(
                    dashboards.data_for_spec,
                    db,
                    settings,
                    runtime,
                    version.spec,
                    item["id"],
                    None,
                    user=viewer.user,
                    include_query=False,
                    today=today,
                )
                entries.append(
                    {
                        "dashboard": board,
                        "title": title,
                        "which": version.which,
                        "number": version.number,
                        "item": item["id"],
                        "itemTitle": item.get("title") or f"{item['visual']} {item['id']}",
                        "viewer": viewer.key,
                        "now": _run(call),
                    }
                )

    return {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "createdAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "simulated": False,
        "datasets": {
            name: {"snapshot": s.table.snapshot_id} for name, s in sorted(workspace.sources.items())
        },
        "viewers": {viewer.key: viewer.label for viewer in people},
        "dashboards": len(boards),
        "items": entries,
    }


# ---- the reports --------------------------------------------------------------------


def _number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int | float) else str(value)


def _change(now: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    if "error" in after:
        return f"cannot be shown: {after['error']}"
    if "error" in now:
        return f"could not be shown before ({now['error']}), and can now"
    if after.get("empty") or now.get("empty"):
        return "its query changes"
    parts = []
    if now["rows"] != after["rows"]:
        parts.append(f"{_number(now['rows'])} rows, then {_number(after['rows'])}")
    else:
        parts.append(f"{_number(now['rows'])} rows, with other values")
    for name, total in now.get("totals", {}).items():
        later = after.get("totals", {}).get(name)
        if later != total:
            parts.append(f"{name} {_number(total)}, then {_number(later)}")
    return "; ".join(parts)


def _heading(entry: Mapping[str, Any]) -> str:
    return f"{entry['title']} ({entry['which']}, version {entry['number']})"


def _counted(number: int, one: str, many: str | None = None) -> str:
    return f"{number:,} {one if number == 1 else (many or one + 's')}"


def report(result: Mapping[str, Any]) -> str:
    """What a digest recorded, in Markdown."""
    viewers_ = result["viewers"]
    entries = result["items"]
    people = sum(1 for key in viewers_ if key.startswith("user:"))
    shared = sum(1 for key in viewers_ if key.startswith("link:"))
    versions_ = len({(e["dashboard"], e["which"]) for e in entries})
    items = len({(e["dashboard"], e["which"], e["item"]) for e in entries})
    empty = result["dashboards"] - len({e["dashboard"] for e in entries})
    nothing = (
        f" {_counted(empty, 'dashboard')} {'has' if empty == 1 else 'have'} no item that "
        "shows data, so nothing to record."
        if empty > 0
        else ""
    )
    lines = [
        "# Results digest",
        "",
        f"Made {result['createdAt']}, counting relative dates from {result['today']}. "
        "Nothing was changed.",
        "",
        f"Recorded {_counted(result['dashboards'], 'dashboard')} at their default filters, as an "
        f"admin, {_counted(people, 'person', 'people')} and {_counted(shared, 'share link')}, "
        f"each on the versions they can open: {_counted(items, 'item')} that show data, in "
        f"{_counted(versions_, 'dashboard version')}, "
        f"{_counted(len(entries), 'result')}.{nothing} After a change, "
        "`results-digest --compare` with this file lists every result that changed.",
        "",
    ]
    failed = [e for e in entries if "error" in e["now"]]
    if failed:
        lines += ["## Not checked", "", "These items cannot be shown:", ""]
        for message, labels in _grouped(failed).items():
            lines.append(f"- {message}: {', '.join(labels)}")
        lines.append("")
    return "\n".join(lines)


def _grouped(entries: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        label = f"{entry['itemTitle']} in {_heading(entry)}"
        names = grouped.setdefault(entry["now"]["error"], [])
        if label not in names:
            names.append(label)
    return grouped


def compare(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Every result that changed since `previous`, and, when `previous` was made
    before the upgrade (with a simulation), whether its report predicted it. Also,
    then, the results that the report said would change and did not (`missed`): a
    rule that should now reach a table and does not would show there."""
    keys = ("dashboard", "which", "item", "viewer")
    before = {tuple(e[k] for k in keys): e for e in previous["items"]}
    changes, added, missed = [], [], []
    for entry in current["items"]:
        earlier = before.pop(tuple(entry[k] for k in keys), None)
        if earlier is None:
            added.append(entry)
            continue
        about = {**entry, "before": earlier["now"], "edited": earlier["number"] != entry["number"]}
        if not same(earlier["now"], entry["now"]):
            predicted = same(earlier["after"], entry["now"]) if "after" in earlier else None
            changes.append({**about, "predicted": predicted})
        elif "after" in earlier and not same(earlier["after"], entry["now"]):
            missed.append({**about, "expected": earlier["after"]})
    refreshed = sorted(
        name
        for name, data in current["datasets"].items()
        if previous["datasets"].get(name, {}).get("snapshot") != data["snapshot"]
    )
    return {
        "changes": changes,
        "missed": missed,
        "added": added,
        "gone": list(before.values()),
        "refreshed": refreshed,
        "viewers": {**previous["viewers"], **current["viewers"]},
    }


def compare_report(previous: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    found = compare(previous, current)
    viewers_ = found["viewers"]
    lines = [
        "# Results compared",
        "",
        f"Made {current['createdAt']}, against the digest of {previous['createdAt']}, "
        f"counting relative dates from {current['today']} as it did.",
        "",
    ]
    if found["refreshed"]:
        lines += [
            "Refreshed since: " + ", ".join(found["refreshed"]) + ". Their new data can change "
            "results too.",
            "",
        ]
    changes, missed = found["changes"], found["missed"]
    if not changes:
        lines += ["**No result changed.**", ""]
    elif previous.get("simulated"):
        unexpected = [c for c in changes if not c["predicted"]]
        lines += [
            f"**{len(changes)} results changed: {len(changes) - len(unexpected)} as the upgrade "
            f"report predicted, {len(unexpected)} not.**",
            "",
        ]
    else:
        lines += [f"**{len(changes)} results changed.**", ""]
    if missed:
        lines += [
            f"**{len(missed)} results did not change, though the upgrade report said they "
            "would.** Check the rules on the tables these items use.",
            "",
            "## Predicted, but unchanged",
            "",
        ]
        for entry in missed:
            how = _change(entry["before"], entry["expected"])
            lines.append(
                f"- {_heading(entry)}, {entry['itemTitle']}, as {viewers_[entry['viewer']]}: "
                f"the report said {how}"
            )
        lines.append("")
    if changes:
        unexpected = [c for c in changes if not c["predicted"]]
        expected = [c for c in changes if c["predicted"]]
        groups = (
            (("Not predicted", unexpected), ("As predicted", expected))
            if previous.get("simulated")
            else (("Changed", changes),)
        )
        for title, group in groups:
            if not group:
                continue
            lines += [f"## {title}", ""]
            for change in group:
                note = " (the dashboard was edited since)" if change["edited"] else ""
                how = _change(change["before"], change["now"])
                lines.append(
                    f"- {_heading(change)}, {change['itemTitle']}, as "
                    f"{viewers_[change['viewer']]}: {how}{note}"
                )
            lines.append("")
    if found["added"] or found["gone"]:
        lines += [
            f"Not compared: {len(found['added'])} results that are new (a new dashboard, item, "
            f"version or viewer) and {len(found['gone'])} that are gone.",
            "",
        ]
    return "\n".join(lines)
