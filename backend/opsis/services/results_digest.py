"""The upgrade check for v2 (ADR-031, V2_DESIGN section 1.4): `python -m opsis
results-digest`. It reads, and changes nothing.

For every dashboard, draft and published, and every item that shows data, it runs the
item at its default filters as each viewer: an admin, every active person who may
open that version, and every live share link (published versions only, never exempt).
Each result is hashed twice: as it is now, and as it will be once row-level security
travels through relationships (rule R2 of ADR-032, simulated by
`semantic.relationships`). The report lists who will see fewer rows, in which items
and tables, and why; and what the migration must resolve first.

`--compare FILE` runs again, counting relative dates from the day FILE was made, and
lists every item whose rows changed since, with who saw it and whether the report had
predicted it.
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
from sqlglot import exp

from opsis.auth.principal import Principal
from opsis.db.models import Dashboard, DashboardVersion, Dataset, ShareLink, User
from opsis.errors import ApiError
from opsis.semantic import relationships as rels
from opsis.semantic.compiler import Scan, Source, UserContext, secured_scan
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


def _count(
    settings: Settings, runtime: QueryRuntime, source: Source, scan: exp.Select
) -> int | str:
    sql = exp.select(exp.Count(this=exp.Star())).from_(scan.subquery("t")).sql(dialect="duckdb")
    try:
        return int(queries.run_raw(settings, runtime, source, sql, 1)[0][0])
    except ApiError as exc:
        return f"{exc.code}: {exc.message}"


def _tables(
    settings: Settings,
    runtime: QueryRuntime,
    sources: Mapping[str, Source],
    relationships: Sequence[rels.Relationship],
    r2: Scan,
    people: Sequence[Viewer],
) -> list[dict[str, Any]]:
    """For each viewer, each table whose visible rows R2 changes: how many now, how
    many after, and the rules that do it."""
    edges: dict[str, list[str]] = {}
    for rel in relationships:
        if rel.active:
            edges.setdefault(rel.from_table, []).append(rel.to_table)
    found: list[dict[str, Any]] = []
    for viewer in people:
        for name in sorted(sources):
            source = sources[name]
            now, _ = secured_scan(source, viewer.user)
            try:
                after, _ = r2(source, viewer.user)
            except ApiError as exc:
                found.append({"viewer": viewer.key, "table": name, "error": exc.message})
                continue
            if after.sql(dialect="duckdb") == now.sql(dialect="duckdb"):
                continue
            ruled = sorted(
                other
                for other in rels.reachable(name, edges)
                if other in sources and queries.rls_applies(sources[other], viewer.user)
            )
            found.append(
                {
                    "viewer": viewer.key,
                    "table": name,
                    "now": _count(settings, runtime, source, now),
                    "after": _count(settings, runtime, source, after),
                    "by": ruled,
                }
            )
    return found


def digest(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    *,
    today: date | None = None,
    simulate: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Every item's result as each viewer sees it now and, with `simulate`, once R2
    applies. Reads only."""
    today = today or settings.today()
    names = set(db.scalars(select(Dataset.name)))
    sources = queries.load_sources(db, settings, names)
    models = {name: source.model for name, source in sources.items()}
    relationships = rels.from_v1_joins(models)
    r2 = rels.relationship_scan(sources, relationships) if simulate else None
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
                entry: dict[str, Any] = {
                    "dashboard": board,
                    "title": title,
                    "which": version.which,
                    "number": version.number,
                    "item": item["id"],
                    "itemTitle": item.get("title") or f"{item['visual']} {item['id']}",
                    "viewer": viewer.key,
                    "now": _run(partial(call, scan=None)),
                }
                if r2 is not None:
                    entry["after"] = _run(partial(call, scan=r2))
                entries.append(entry)

    result: dict[str, Any] = {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "createdAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "simulated": simulate,
        "datasets": {
            name: {"snapshot": s.table.snapshot_id} for name, s in sorted(sources.items())
        },
        "viewers": {viewer.key: viewer.label for viewer in people},
        "dashboards": len(boards),
        "items": entries,
    }
    if r2 is not None:
        result["relationships"] = [
            {"id": rel.id, "describe": rel.describe(), "active": rel.active}
            for rel in relationships
        ]
        result["loops"] = [list(loop) for loop in rels.refused_loops(relationships, models)]
        result["tables"] = _tables(settings, runtime, sources, relationships, r2, people)
    return result


# ---- the reports --------------------------------------------------------------------


def _number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int | float) else str(value)


def _change(now: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    if "error" in after:
        return f"cannot be shown after the upgrade: {after['error']}"
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


def report(result: Mapping[str, Any]) -> str:
    """The upgrade report, in Markdown."""
    viewers_ = result["viewers"]
    entries = result["items"]
    checked = [e for e in entries if "hash" in e["now"]]
    changed = [e for e in checked if "after" in e and not same(e["now"], e["after"])]
    people = sum(1 for key in viewers_ if key.startswith("user:"))
    shared = sum(1 for key in viewers_ if key.startswith("link:"))
    versions_ = len({(e["dashboard"], e["which"]) for e in entries})
    items = len({(e["dashboard"], e["which"], e["item"]) for e in entries})
    lines = [
        "# Upgrade report: row-level security through relationships",
        "",
        f"Made {result['createdAt']}, counting relative dates from {result['today']}. "
        "Nothing was changed.",
        "",
        f"Checked {result['dashboards']} dashboards ({versions_} versions, {items} items that "
        f"show data) at their default filters, as an admin, {people} people and {shared} share "
        f"links, each person and link on the versions they can open: {len(entries)} results.",
        "",
    ]
    if not changed:
        lines += ["**Nobody will see fewer rows.** Every result stays the same.", ""]
    else:
        who = sorted({e["viewer"] for e in changed})
        lines += [
            f"**{len(who)} of {len(viewers_)} viewers will see fewer rows, in "
            f"{len({(e['dashboard'], e['item']) for e in changed})} items of "
            f"{len({e['dashboard'] for e in changed})} dashboards.**",
            "",
            "## Who will see fewer rows",
            "",
        ]
        by_board: dict[str, list[Mapping[str, Any]]] = {}
        for entry in changed:
            by_board.setdefault(_heading(entry), []).append(entry)
        for heading, group in by_board.items():
            lines += [f"### {heading}", ""]
            for viewer in sorted({e["viewer"] for e in group}, key=lambda k: viewers_[k]):
                lines.append(f"- **{viewers_[viewer]}**")
                for entry in (e for e in group if e["viewer"] == viewer):
                    lines.append(
                        f"  - {entry['itemTitle']}: {_change(entry['now'], entry['after'])}"
                    )
            lines.append("")
    tables = [t for t in result.get("tables", []) if "error" not in t]
    if tables:
        lines += [
            "## Tables each viewer will see fewer rows of",
            "",
            "| Viewer | Table | Rows now | After the upgrade | Because of the rules on |",
            "|---|---|---|---|---|",
        ]
        for row in tables:
            lines.append(
                f"| {viewers_[row['viewer']]} | {row['table']} | {_number(row['now'])} | "
                f"{_number(row['after'])} | {', '.join(row['by'])} |"
            )
        lines.append("")
    loops = result.get("loops") or []
    if loops:
        lines += ["## To resolve before the migration", ""]
        for loop in loops:
            lines.append(
                f"- The relationships {' → '.join(loop)} would form a loop that row-level "
                "security would travel round, which v2 refuses. Change one of these joins to "
                "`one_to_many` on the other model, or remove it, so the loop is broken."
            )
        lines.append("")
    failed = [e for e in entries if "error" in e["now"]]
    if failed:
        lines += ["## Not checked", "", "These items cannot be shown now either:", ""]
        for message, labels in _grouped(failed).items():
            lines.append(f"- {message}: {', '.join(labels)}")
        lines.append("")
    if result.get("relationships"):
        lines += ["## The relationships the migration will make", ""]
        for rel in result["relationships"]:
            state = "active" if rel["active"] else "inactive (carries no security filter)"
            lines.append(f"- {rel['describe']}: {state}")
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
    """Every result that changed since `previous`, and whether its report predicted it."""
    keys = ("dashboard", "which", "item", "viewer")
    before = {tuple(e[k] for k in keys): e for e in previous["items"]}
    changes, added = [], []
    for entry in current["items"]:
        earlier = before.pop(tuple(entry[k] for k in keys), None)
        if earlier is None:
            added.append(entry)
        elif not same(earlier["now"], entry["now"]):
            changes.append(
                {
                    **entry,
                    "before": earlier["now"],
                    "predicted": same(earlier.get("after"), entry["now"]),
                    "edited": earlier["number"] != entry["number"],
                }
            )
    refreshed = sorted(
        name
        for name, data in current["datasets"].items()
        if previous["datasets"].get(name, {}).get("snapshot") != data["snapshot"]
    )
    return {
        "changes": changes,
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
    changes = found["changes"]
    if not changes:
        lines += ["**No result changed.**", ""]
    else:
        unexpected = [c for c in changes if not c["predicted"]]
        lines += [
            f"**{len(changes)} results changed: {len(changes) - len(unexpected)} as the upgrade "
            f"report predicted, {len(unexpected)} not.**",
            "",
        ]
        expected = [c for c in changes if c["predicted"]]
        for title, group in (("Not predicted", unexpected), ("As predicted", expected)):
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
