"""The upgrade check for v2 (`python -m opsis results-digest`, V2_DESIGN section 1.4):
what every dashboard shows each viewer now, and who will see fewer rows once
row-level security travels through relationships (rule R2 of ADR-032).

The fixtures cover every v1 join pattern: direct, two steps, inner, one_to_many
declared, a restricted dataset in between, and two ways to one dataset; plus loops,
admins, share links, drafts, --compare, and that the check changes nothing."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsis import __main__ as cli
from opsis.db.models import AuditLog, Dashboard, DashboardVersion, SemanticModel, ShareLink
from opsis.semantic import relationships as rels
from opsis.semantic.model import parse_model
from opsis.services import demo, results_digest
from tests.test_auth import SERVER_SIGN_IN, admin_client
from tests.test_ingestion import connect, make_dataset, ok, run_jobs
from tests.test_query_api import source as source  # noqa: F401  (fixture)
from tests.test_spec_drafts import _planned_model_migration

ClientFor = Callable[..., TestClient]

DONORS_RULE = [{"filter": "region IN {{ user.attributes.regions }}"}]
REGIONS_RULE = [{"filter": "code IN {{ user.attributes.regions }}"}]
TO_DONORS = {"to": "donors", "on": {"donor_id": "id"}}
TO_REGIONS = {"to": "regions", "on": {"region": "code"}}

KPI = {
    "id": "v_gifts",
    "visual": "kpi",
    "position": {"x": 0, "y": 0, "w": 3, "h": 3},
    "dataset": "gifts",
    "bindings": {"value": ["gifts.row_count"]},
}
BY_TIER = {
    "id": "v_by_tier",
    "visual": "bar",
    "position": {"x": 3, "y": 0, "w": 9, "h": 6},
    "dataset": "gifts",
    "bindings": {"category": ["donors.tier"], "value": ["gifts.row_count"]},
}
NOTE = {"id": "v_note", "visual": "text", "position": {"x": 0, "y": 6, "w": 12, "h": 2},
        "config": {"markdown": "## Notes"}}  # fmt: skip


def spec(title: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {
        "specVersion": 1,
        "title": title,
        "layout": {"columns": 12, "rowHeight": 40},
        "filters": [],
        "items": [copy.deepcopy(item) for item in items],
    }


def model(*fields: str, **extra: Any) -> dict[str, Any]:
    return {
        "dimensions": {name: {"column": name, "type": "string"} for name in fields},
        "measures": {"row_count": {"agg": "count"}},
        **extra,
    }


# ---- relationships from v1 joins -----------------------------------------------------


def _as_planned(found: list[rels.Relationship]) -> dict[str, Any]:
    return {
        rel.id: {
            "from": {"table": rel.from_table, "columns": list(rel.from_columns)},
            "to": {"table": rel.to_table, "columns": list(rel.to_columns)},
            "cardinality": rel.cardinality,
            "crossFilter": "single",
            "securityFilter": "single",
            "active": rel.active,
            "joinType": rel.join_type,
        }
        for rel in found
    }


@pytest.mark.parametrize(
    "models",
    [
        demo.MODELS,
        {"a": {"joins": [{"to": "b", "on": {"x": "y"}, "relationship": "one_to_many"}]}, "b": {}},
        {
            "a": {"joins": [{"to": "b", "on": {"x": "y"}, "relationship": "one_to_many"}]},
            "b": {"joins": [{"to": "a", "on": {"y": "x"}}]},
        },
        {
            "a": {
                "joins": [
                    {"to": "b", "on": {"x": "y"}, "type": "inner", "relationship": "one_to_one"}
                ]
            },
            "b": {"joins": [{"to": "a", "on": {"y": "x"}, "relationship": "one_to_one"}]},
        },  # fmt: skip
        {"a": {"joins": [{"to": "b", "on": {"x": "y"}}, {"to": "b", "on": {"z": "y"}}]}, "b": {}},
    ],
    ids=["demo", "one_to_many only", "both sides", "one_to_one both ways", "two joins to b"],
)
def test_relationships_are_the_planned_migrations(models: dict[str, dict[str, Any]]) -> None:
    parsed = {name: parse_model(name, definition) for name, definition in models.items()}
    planned = _planned_model_migration(models)["relationships"]
    assert _as_planned(rels.from_v1_joins(parsed)) == planned


def test_a_loop_is_refused_only_when_a_rule_could_travel_round_it() -> None:
    def loops(definitions: dict[str, dict[str, Any]]) -> list[tuple[str, ...]]:
        parsed = {name: parse_model(name, d) for name, d in definitions.items()}
        return rels.refused_loops(rels.from_v1_joins(parsed), parsed)

    ab = {"to": "b", "on": {"x": "y"}}
    ba = {"to": "a", "on": {"y": "x"}}
    rule = [{"filter": "x = 'one'"}]
    assert loops({"a": {"joins": [ab]}, "b": {"joins": [ba]}}) == []  # no rules: allowed
    assert loops({"a": {"joins": [ab]}, "b": {"joins": [ba], "rls": rule}}) == [("a", "b", "a")]
    # A rule the loop reaches is refused too: the rows it leaves would depend on themselves.
    bc = {"to": "c", "on": {"y": "z"}}
    assert loops({"a": {"joins": [ab]}, "b": {"joins": [ba, bc]}, "c": {"rls": rule}}) == [
        ("a", "b", "a")
    ]
    # A rule only before the loop is not.
    za = {"to": "a", "on": {"q": "x"}}
    assert (
        loops({"z": {"joins": [za], "rls": rule}, "a": {"joins": [ab]}, "b": {"joins": [ba]}}) == []
    )
    # Two ways to one table is not a loop; a join to itself is.
    two_ways = {"g": {"joins": [{"to": "d", "on": {"d": "id"}}, {"to": "r", "on": {"r": "c"}}]},
                "d": {"joins": [{"to": "r", "on": {"r": "c"}}]}, "r": {"rls": rule}}  # fmt: skip
    assert loops(two_ways) == []
    assert loops({"a": {"joins": [{"to": "a", "on": {"x": "y"}}], "rls": rule}}) == [("a", "a")]
    # one_to_many joins are inactive, and carry nothing round.
    back = {"to": "a", "on": {"y": "x"}, "relationship": "one_to_many"}
    assert loops({"a": {"joins": [ab], "rls": rule}, "b": {"joins": [back]}}) == []


# ---- the digest, on data -------------------------------------------------------------


class World:
    """gifts (2000), donors (40; gifts name 45, so some match nothing) and regions,
    with Ann, a viewer who may see VIC only."""

    def __init__(
        self, client_for: ClientFor, source: dict[str, Any], db: Session, **settings: Any
    ) -> None:  # noqa: F811
        self.db = db
        self.conn = source["conn"]
        self.conn.execute("CREATE TABLE src.regions (code text, manager text)")
        self.conn.execute(
            "INSERT INTO src.regions VALUES ('VIC','Ann'), ('NSW','Ben'), ('QLD','Cat')"
        )
        self.admin = admin_client(client_for, **settings)
        connection = connect(self.admin, "postgres", source["config"])
        self.ids = {
            table: make_dataset(self.admin, connection, f"src.{table}", name=table)["id"]
            for table in ("gifts", "donors", "regions")
        }
        run_jobs(self.admin)
        self.ann = self.user("ann@example.test", "viewer", {"regions": ["VIC"]})

    def user(self, email: str, role: str, attributes: dict[str, Any]) -> str:
        made = self.admin.post(
            "/api/users", json={"email": email, "role": role, "attributes": attributes}
        )
        return f"user:{ok(made, 201)['user']['id']}"

    def models(self, **definitions: dict[str, Any]) -> None:
        for dataset, definition in definitions.items():
            ok(self.admin.put(f"/api/models/{dataset}", json={"definition": definition}))

    def dashboard(self, *items: dict[str, Any], title: str = "Gifts", publish: bool = True) -> str:
        made = self.admin.post(
            "/api/dashboards", json={"title": title, "spec": spec(title, *items)}
        )
        dashboard = str(ok(made, 201)["id"])
        if publish:
            ok(self.admin.post(f"/api/dashboards/{dashboard}/publish"))
        shares = [{"grantee": self.ann, "role": "viewer"}]
        ok(self.admin.put(f"/api/dashboards/{dashboard}/access", json={"shares": shares}))
        return dashboard

    def digest(self, **options: Any) -> dict[str, Any]:
        app: Any = self.admin.app
        return results_digest.digest(
            self.db, app.state.settings, app.state.query_runtime, **options
        )

    def count(self, sql: str) -> int:
        return int(self.conn.execute(sql).fetchone()[0])


ALL = "SELECT count(*) FROM src.gifts"
VIC_DONORS = (
    "SELECT count(*) FROM src.gifts g JOIN src.donors d ON d.id = g.donor_id WHERE d.region = 'VIC'"
)
VIC_BOTH = VIC_DONORS + " AND g.region = 'VIC'"


def entry(
    found: dict[str, Any], item: str, viewer: str, which: str = "draft and published"
) -> dict[str, Any]:
    [hit] = [
        e for e in found["items"] if (e["item"], e["viewer"], e["which"]) == (item, viewer, which)
    ]
    return hit


def total(outcome: dict[str, Any]) -> float:
    return float(outcome["totals"]["gifts.row_count"])


def changed_viewers(found: dict[str, Any]) -> set[str]:
    return {e["viewer"] for e in found["items"] if not results_digest.same(e["now"], e["after"])}


@pytest.fixture
def world(client_for: ClientFor, source: dict[str, Any], db: Session) -> World:  # noqa: F811
    return World(client_for, source, db)


def test_direct_a_rule_on_donors_hides_other_donors_gifts(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", rls=DONORS_RULE),
    )
    world.dashboard(KPI, BY_TIER, NOTE)
    found = world.digest()

    kpi = entry(found, "v_gifts", world.ann)
    assert total(kpi["now"]) == world.count(ALL) == 2000  # v1: the rule does not reach gifts
    assert total(kpi["after"]) == world.count(VIC_DONORS)  # and gifts of unknown donors go too
    by_tier = entry(found, "v_by_tier", world.ann)
    # v1 shows the other gifts under an empty tier; after the upgrade they are gone.
    assert (by_tier["now"]["rows"], by_tier["after"]["rows"]) == (3, 2)
    assert total(by_tier["now"]) == 2000 and total(by_tier["after"]) == world.count(VIC_DONORS)
    assert not any(e["item"] == "v_note" for e in found["items"])  # text shows no data
    # Admins are exempt, so nothing changes for them.
    assert changed_viewers(found) == {world.ann}
    assert results_digest.same(
        entry(found, "v_gifts", "admin")["now"], entry(found, "v_gifts", "admin")["after"]
    )

    [table] = [t for t in found["tables"] if t["viewer"] == world.ann and t["table"] == "gifts"]
    assert (table["now"], table["after"], table["by"]) == (
        2000,
        world.count(VIC_DONORS),
        ["donors"],
    )
    text = results_digest.report(found)
    label = found["viewers"][world.ann]
    assert (
        f"**1 of {len(found['viewers'])} viewers will see fewer rows, in 2 items of 1 dashboards.**"
        in text
    )
    assert (
        f"- **{label}**\n  - kpi v_gifts: 1 rows, with other values; gifts.row_count 2,000, then {world.count(VIC_DONORS):,}"
        in text
    )
    assert "  - bar v_by_tier: 3 rows, then 2;" in text
    assert f"| {label} | gifts | 2,000 | {world.count(VIC_DONORS):,} | donors |" in text
    assert "- gifts (donor_id) to donors (id): active" in text
    assert "## To resolve" not in text and "## Not checked" not in text


def test_two_steps_a_rule_on_regions_reaches_gifts_through_donors(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
        regions=model("code", "manager", rls=REGIONS_RULE),
    )
    world.dashboard(KPI, BY_TIER)
    found = world.digest()
    assert total(entry(found, "v_gifts", world.ann)["after"]) == world.count(VIC_DONORS)
    counts = {
        t["table"]: (t["now"], t["after"], t["by"])
        for t in found["tables"]
        if t["viewer"] == world.ann
    }
    vic_donors = world.count("SELECT count(*) FROM src.donors WHERE region = 'VIC'")
    assert counts == {
        "donors": (40, vic_donors, ["regions"]),
        "gifts": (2000, world.count(VIC_DONORS), ["regions"]),
    }


def test_inner_a_query_that_already_joins_does_not_change(world: World) -> None:
    inner = {**TO_DONORS, "type": "inner"}
    world.models(
        gifts=model("fund", "region", joins=[inner]),
        donors=model("tier", "region", rls=DONORS_RULE),
    )
    world.dashboard(KPI, BY_TIER)
    found = world.digest()
    # v1's inner join with the donors rule already hid the other donors' gifts ...
    by_tier = entry(found, "v_by_tier", world.ann)
    assert results_digest.same(by_tier["now"], by_tier["after"])
    assert total(by_tier["now"]) == world.count(VIC_DONORS)
    # ... but a query of gifts alone did not join, and changes.
    kpi = entry(found, "v_gifts", world.ann)
    assert (total(kpi["now"]), total(kpi["after"])) == (2000, world.count(VIC_DONORS))
    assert "bar v_by_tier" not in results_digest.report(found)


def test_one_to_many_declared_becomes_inactive_and_changes_nothing(world: World) -> None:
    back = {"to": "gifts", "on": {"id": "donor_id"}, "relationship": "one_to_many"}
    world.models(
        gifts=model("fund", "region"), donors=model("tier", "region", joins=[back], rls=DONORS_RULE)
    )
    world.dashboard(KPI)
    found = world.digest()
    assert changed_viewers(found) == set() and found["tables"] == []
    text = results_digest.report(found)
    assert "**Nobody will see fewer rows.**" in text
    assert "- gifts (donor_id) to donors (id): inactive (carries no security filter)" in text


def test_a_restricted_dataset_in_between_still_filters(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
        regions=model("code", "manager", rls=REGIONS_RULE),
    )
    # Ann may not use donors: v1 never takes a path through it, and R2 reads it only to
    # filter, with no access check.
    ok(world.admin.put(f"/api/datasets/{world.ids['donors']}/access", json={"restricted": True}))
    world.dashboard(KPI)
    found = world.digest()
    kpi = entry(found, "v_gifts", world.ann)
    assert (total(kpi["now"]), total(kpi["after"])) == (2000, world.count(VIC_DONORS))


def test_two_ways_to_one_dataset_both_filter(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS, TO_REGIONS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
        regions=model("code", "manager", rls=REGIONS_RULE),
    )
    ben = world.user("ben@example.test", "viewer", {"regions": ["VIC", "NSW"]})
    dashboard = world.dashboard(KPI)
    shares = [{"grantee": who, "role": "viewer"} for who in (world.ann, ben)]
    ok(world.admin.put(f"/api/dashboards/{dashboard}/access", json={"shares": shares}))
    found = world.digest()
    # A gift stays when its own region and its donor's region are both visible. In this
    # data the two always differ, so Ann (VIC only) keeps no gift at all.
    assert total(entry(found, "v_gifts", world.ann)["after"]) == world.count(VIC_BOTH) == 0
    two = ("'VIC'", "('VIC','NSW')")
    either = world.count(VIC_DONORS.replace("= " + two[0], "IN " + two[1]))
    both = world.count(VIC_BOTH.replace("= " + two[0], "IN " + two[1]))
    assert total(entry(found, "v_gifts", ben)["after"]) == both
    assert 0 < both < either


def test_a_loop_is_listed_to_resolve_first(world: World) -> None:
    back = {"to": "donors", "on": {"manager": "region"}}
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
        regions=model("code", "manager", joins=[back], rls=REGIONS_RULE),
    )
    world.dashboard(KPI)
    found = world.digest()
    assert found["loops"] == [["donors", "regions", "donors"]]
    kpi = entry(found, "v_gifts", world.ann)
    assert "relationship_loop" in kpi["after"]["error"]
    text = results_digest.report(found)
    assert (
        "## To resolve before the migration\n\n- The relationships donors → regions → donors"
        in text
    )
    assert "kpi v_gifts: cannot be shown after the upgrade: relationship_loop:" in text


def test_viewers_versions_and_share_links(
    client_for: ClientFor, source: dict[str, Any], db: Session
) -> None:  # noqa: F811
    world = World(client_for, source, db, **SERVER_SIGN_IN)
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", rls=DONORS_RULE),
    )
    cat = world.user("cat@example.test", "editor", {"regions": ["NSW"]})
    boss = world.user("boss@example.test", "admin", {})
    dashboard = world.dashboard(KPI)
    shares = [{"grantee": world.ann, "role": "viewer"}, {"grantee": cat, "role": "editor"}]
    ok(world.admin.put(f"/api/dashboards/{dashboard}/access", json={"shares": shares}))
    # A newer draft beside the published version.
    ok(
        world.admin.put(
            f"/api/dashboards/{dashboard}",
            json={"spec": spec("Gifts", KPI, BY_TIER), "baseVersion": 1},
        )
    )
    made = {"name": "Board", "attributes": {"regions": ["QLD"]}}
    link = ok(world.admin.post(f"/api/dashboards/{dashboard}/links", json=made), 201)
    revoked = ok(world.admin.post(f"/api/dashboards/{dashboard}/links", json={"name": "Old"}), 201)
    ok(world.admin.post(f"/api/links/{revoked['id']}/revoke"))
    private = world.dashboard(KPI, title="Not shared")
    ok(world.admin.put(f"/api/dashboards/{private}/access", json={"shares": []}))

    found = world.digest()
    board = f"link:{link['id']}"
    seen = {(e["title"], e["which"], e["item"], e["viewer"]) for e in found["items"]}
    assert set(found["viewers"]) >= {"admin", world.ann, cat, boss, board}
    assert f"link:{revoked['id']}" not in found["viewers"]
    published = {
        ("Gifts", "published", "v_gifts", v) for v in ("admin", world.ann, cat, boss, board)
    }
    draft = {
        ("Gifts", "draft", item, v)
        for item in ("v_gifts", "v_by_tier")
        for v in ("admin", cat, boss)
    }
    assert published | draft <= seen
    # Ann and the link see the published version only; nobody but admins sees "Not shared".
    assert {(t, w) for t, w, _, v in seen if v in (world.ann, board)} == {("Gifts", "published")}
    admins = {
        key
        for key, label in found["viewers"].items()
        if key == "admin" or label.endswith("(admin)")
    }
    assert {v for t, _, _, v in seen if t == "Not shared"} == admins
    # Admins are exempt (the badge shows it); a link never is, whoever made it.
    assert changed_viewers(found) == {world.ann, cat, board}
    after = entry(found, "v_gifts", board, "published")["after"]
    assert total(after) == world.count(VIC_DONORS.replace("'VIC'", "'QLD'"))


def test_it_changes_nothing_and_compare_finds_nothing_after_no_change(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
    db: Session,
    tmp_path: Path,
) -> None:
    world = World(client_for, source, db, **SERVER_SIGN_IN)  # share links need server mode
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", rls=DONORS_RULE),
    )
    dashboard = world.dashboard(KPI, BY_TIER)
    ok(world.admin.post(f"/api/dashboards/{dashboard}/links", json={"name": "Board"}), 201)

    def state() -> list[Any]:
        counts = [
            world.db.scalar(select(func.count()).select_from(t))
            for t in (AuditLog, Dashboard, DashboardVersion, SemanticModel)
        ]
        views = list(world.db.scalars(select(ShareLink.view_count)))
        world.db.rollback()
        return [counts, views]

    before = state()
    settings: Any = world.admin.app.state.settings  # type: ignore[attr-defined]
    out = tmp_path / "digest.json"
    cli._results_digest(settings, ["--out", str(out), "--today", "2026-06-30"])
    assert state() == before
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["format"] == results_digest.FORMAT and saved["today"] == "2026-06-30"
    assert out.with_suffix(".md").read_text(encoding="utf-8").startswith("# Upgrade report")

    # Again, after nothing changed: --compare finds no difference.
    again = tmp_path / "compared.json"
    cli._results_digest(settings, ["--out", str(again), "--compare", str(out)])
    text = again.with_suffix(".md").read_text(encoding="utf-8")
    assert "**No result changed.**" in text and "counting relative dates from 2026-06-30" in text
    assert json.loads(again.read_text(encoding="utf-8"))["simulated"] is False
    assert state() == before

    with pytest.raises(SystemExit, match="usage"):
        cli._results_digest(settings, ["--out"])
    with pytest.raises(SystemExit, match="Cannot read"):
        cli._results_digest(settings, ["--compare", str(out.with_suffix(".md"))])
    other = tmp_path / "other.json"
    other.write_text('{"format": "something else"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="is not a results digest"):
        cli._results_digest(settings, ["--compare", str(other)])


def test_compare_says_which_changes_were_predicted() -> None:
    def outcome(digest: str, rows: int = 1) -> dict[str, Any]:
        return {
            "hash": digest,
            "rows": rows,
            "truncated": False,
            "totals": {"gifts.row_count": float(rows)},
        }

    def result(entries: list[dict[str, Any]], snapshot: str) -> dict[str, Any]:
        return {
            "createdAt": "2026-11-26T10:00:00+00:00",
            "today": "2026-11-26",
            "datasets": {"gifts": {"snapshot": snapshot}},
            "viewers": {"admin": "an admin", "user:1": "Ann"},
            "items": entries,
        }

    def item(name: str, viewer: str, **outcomes: Any) -> dict[str, Any]:
        return {"dashboard": "d1", "title": "Gifts", "which": "published", "number": 1,
                "item": name, "itemTitle": name, "viewer": viewer, **outcomes}  # fmt: skip

    previous = result(
        [
            item("same", "user:1", now=outcome("a"), after=outcome("a")),
            item("predicted", "user:1", now=outcome("b", 3), after=outcome("c", 2)),
            item("surprise", "admin", now=outcome("d"), after=outcome("d")),
            item("gone", "admin", now=outcome("e"), after=outcome("e")),
        ],
        "s1",
    )
    current = result(
        [
            item("same", "user:1", now=outcome("a")),
            item("predicted", "user:1", now=outcome("c", 2)),
            item("surprise", "admin", now=outcome("x", 5)),
            item("new", "admin", now=outcome("f")),
        ],
        "s2",
    )
    found = results_digest.compare(previous, current)
    assert [(c["item"], c["predicted"]) for c in found["changes"]] == [
        ("predicted", True),
        ("surprise", False),
    ]
    assert [e["item"] for e in found["added"]] == ["new"] and [
        e["item"] for e in found["gone"]
    ] == ["gone"]
    text = results_digest.compare_report(previous, current)
    assert "Refreshed since: gifts." in text
    assert "**2 results changed: 1 as the upgrade report predicted, 1 not.**" in text
    assert (
        "## Not predicted\n\n- Gifts (published, version 1), surprise, as an admin: 1 rows, then 5;"
        in text
    )
    assert (
        "## As predicted\n\n- Gifts (published, version 1), predicted, as Ann: 3 rows, then 2;"
        in text
    )
    assert "Not compared: 1 results that are new" in text
