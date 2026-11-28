"""The results check (`python -m opsis results-digest`, V2_DESIGN section 1.4), after
the workspace model's migration: what every dashboard shows each viewer, and with
`--compare`, every result that changed since an earlier digest, and whether the
upgrade report made before the migration predicted it.

The fixtures cover every v1 join pattern, now relationships: direct, two steps,
inner, one_to_many declared, a restricted dataset in between, and two ways to one
dataset. Each checks that people now see what the upgrade report predicted (row-level
security travels through relationships, rule R2 of ADR-032), against SQL on the
source. Plus loops, admins, share links, drafts, --compare, and that the check
changes nothing."""

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
from opsis.db.models import AuditLog, Dashboard, DashboardVersion, ShareLink, WorkspaceModelVersion
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
        """Written through the v1 routes, one table at a time, as a migrated model is:
        each join becomes a relationship by the migration's rules."""
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

    # As the upgrade report predicted: gifts of other donors, and of unknown ones, are gone.
    kpi = entry(found, "v_gifts", world.ann)
    assert total(kpi["now"]) == world.count(VIC_DONORS) < world.count(ALL)
    by_tier = entry(found, "v_by_tier", world.ann)
    # v1 showed the other gifts under an empty tier; now there is no such row.
    assert by_tier["now"]["rows"] == 2 and total(by_tier["now"]) == world.count(VIC_DONORS)
    assert not any(e["item"] == "v_note" for e in found["items"])  # text shows no data
    # Admins are exempt.
    assert total(entry(found, "v_gifts", "admin")["now"]) == world.count(ALL) == 2000

    text = results_digest.report(found)
    assert text.startswith("# Results digest\n")
    assert "Recorded 1 dashboard at their default filters, as an admin" in text
    assert "2 items that show data, in 1 dashboard version, " in text
    assert "no item that shows data" not in text
    assert "## Not checked" not in text


def test_two_steps_a_rule_on_regions_reaches_gifts_through_donors(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
        regions=model("code", "manager", rls=REGIONS_RULE),
    )
    world.dashboard(KPI, BY_TIER)
    found = world.digest()
    assert total(entry(found, "v_gifts", world.ann)["now"]) == world.count(VIC_DONORS)
    assert total(entry(found, "v_by_tier", world.ann)["now"]) == world.count(VIC_DONORS)


def test_inner_a_query_that_already_joined_and_one_that_did_not(world: World) -> None:
    inner = {**TO_DONORS, "type": "inner"}
    world.models(
        gifts=model("fund", "region", joins=[inner]),
        donors=model("tier", "region", rls=DONORS_RULE),
    )
    world.dashboard(KPI, BY_TIER)
    found = world.digest()
    # v1's inner join with the donors rule already hid the other donors' gifts, and a
    # query of gifts alone (which v1 did not join) now hides them too.
    for item in ("v_by_tier", "v_gifts"):
        assert total(entry(found, item, world.ann)["now"]) == world.count(VIC_DONORS)


def test_one_to_many_declared_becomes_inactive_and_carries_nothing(world: World) -> None:
    back = {"to": "gifts", "on": {"id": "donor_id"}, "relationship": "one_to_many"}
    world.models(
        gifts=model("fund", "region"), donors=model("tier", "region", joins=[back], rls=DONORS_RULE)
    )
    relationships = ok(world.admin.get("/api/model"))["document"]["relationships"]
    assert relationships["gifts__donors"]["active"] is False
    world.dashboard(KPI)
    found = world.digest()
    assert total(entry(found, "v_gifts", world.ann)["now"]) == world.count(ALL)


def test_a_restricted_dataset_in_between_still_filters(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
        regions=model("code", "manager", rls=REGIONS_RULE),
    )
    # Ann may not use donors: R2 reads it only to filter, with no access check.
    ok(world.admin.put(f"/api/datasets/{world.ids['donors']}/access", json={"restricted": True}))
    world.dashboard(KPI)
    found = world.digest()
    assert total(entry(found, "v_gifts", world.ann)["now"]) == world.count(VIC_DONORS)


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
    assert total(entry(found, "v_gifts", world.ann)["now"]) == world.count(VIC_BOTH) == 0
    two = ("'VIC'", "('VIC','NSW')")
    either = world.count(VIC_DONORS.replace("= " + two[0], "IN " + two[1]))
    both = world.count(VIC_BOTH.replace("= " + two[0], "IN " + two[1]))
    assert total(entry(found, "v_gifts", ben)["now"]) == both
    assert 0 < both < either


def test_a_loop_through_a_table_with_rules_cannot_be_saved(world: World) -> None:
    world.models(
        gifts=model("fund", "region", joins=[TO_DONORS]),
        donors=model("tier", "region", joins=[TO_REGIONS]),
    )
    back = {"to": "donors", "on": {"manager": "region"}}
    refused = world.admin.put(
        "/api/models/regions",
        json={"definition": model("code", "manager", joins=[back], rls=REGIONS_RULE)},
    )
    assert refused.status_code == 422
    [problem] = refused.json()["error"]["details"]
    assert "donors → regions → donors form a loop that row-level security" in problem["message"]
    # Without the rule the loop is allowed.
    ok(
        world.admin.put(
            "/api/models/regions", json={"definition": model("code", "manager", joins=[back])}
        )
    )


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
    # Admins are exempt; a link never is, whoever made it.
    viewers = {v for t, w, i, v in seen if (t, w, i) == ("Gifts", "published", "v_gifts")}
    counts = {v: total(entry(found, "v_gifts", v, "published")["now"]) for v in viewers}
    assert {v for v, n in counts.items() if n < world.count(ALL)} == {world.ann, cat, board}
    assert counts[board] == world.count(VIC_DONORS.replace("'VIC'", "'QLD'"))
    assert counts[cat] == world.count(VIC_DONORS.replace("'VIC'", "'NSW'"))


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
            for t in (AuditLog, Dashboard, DashboardVersion, WorkspaceModelVersion)
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
    assert saved["simulated"] is False
    assert out.with_suffix(".md").read_text(encoding="utf-8").startswith("# Results digest")

    # Again, after nothing changed: --compare finds no difference.
    again = tmp_path / "compared.json"
    cli._results_digest(settings, ["--out", str(again), "--compare", str(out)])
    text = again.with_suffix(".md").read_text(encoding="utf-8")
    assert "**No result changed.**" in text and "counting relative dates from 2026-06-30" in text
    assert "Predicted, but unchanged" not in text
    assert state() == before

    with pytest.raises(SystemExit, match="usage"):
        cli._results_digest(settings, ["--out"])
    with pytest.raises(SystemExit, match="Cannot read"):
        cli._results_digest(settings, ["--compare", str(out.with_suffix(".md"))])
    other = tmp_path / "other.json"
    other.write_text('{"format": "something else"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="is not a results digest"):
        cli._results_digest(settings, ["--compare", str(other)])


def _outcome(digest: str, rows: int = 1) -> dict[str, Any]:
    return {"hash": digest, "rows": rows, "truncated": False,
            "totals": {"gifts.row_count": float(rows)}}  # fmt: skip


def _result(entries: list[dict[str, Any]], snapshot: str, simulated: bool) -> dict[str, Any]:
    return {
        "createdAt": "2026-11-26T10:00:00+00:00",
        "today": "2026-11-26",
        "simulated": simulated,
        "datasets": {"gifts": {"snapshot": snapshot}},
        "viewers": {"admin": "an admin", "user:1": "Ann"},
        "items": entries,
    }


def _item(name: str, viewer: str, **outcomes: Any) -> dict[str, Any]:
    return {"dashboard": "d1", "title": "Gifts", "which": "published", "number": 1,
            "item": name, "itemTitle": name, "viewer": viewer, **outcomes}  # fmt: skip


def test_compare_with_the_upgrade_report_says_what_it_predicted() -> None:
    previous = _result(
        [
            _item("same", "user:1", now=_outcome("a"), after=_outcome("a")),
            _item("predicted", "user:1", now=_outcome("b", 3), after=_outcome("c", 2)),
            _item("surprise", "admin", now=_outcome("d"), after=_outcome("d")),
            _item("missed", "user:1", now=_outcome("g", 4), after=_outcome("h", 1)),
            _item("gone", "admin", now=_outcome("e"), after=_outcome("e")),
        ],
        "s1",
        simulated=True,
    )
    current = _result(
        [
            _item("same", "user:1", now=_outcome("a")),
            _item("predicted", "user:1", now=_outcome("c", 2)),
            _item("surprise", "admin", now=_outcome("x", 5)),
            _item("missed", "user:1", now=_outcome("g", 4)),
            _item("new", "admin", now=_outcome("f")),
        ],
        "s2",
        simulated=False,
    )
    found = results_digest.compare(previous, current)
    assert [(c["item"], c["predicted"]) for c in found["changes"]] == [
        ("predicted", True),
        ("surprise", False),
    ]
    # Said to change, and did not: a rule that should now reach a table and does not.
    assert [m["item"] for m in found["missed"]] == ["missed"]
    assert [e["item"] for e in found["added"]] == ["new"] and [
        e["item"] for e in found["gone"]
    ] == ["gone"]
    text = results_digest.compare_report(previous, current)
    assert "Refreshed since: gifts." in text
    assert "**2 results changed: 1 as the upgrade report predicted, 1 not.**" in text
    assert "**1 results did not change, though the upgrade report said they would.**" in text
    assert (
        "## Predicted, but unchanged\n\n- Gifts (published, version 1), missed, as Ann: the "
        "report said 4 rows, then 1;"
    ) in text
    assert (
        "## Not predicted\n\n- Gifts (published, version 1), surprise, as an admin: 1 rows, then 5;"
        in text
    )
    assert (
        "## As predicted\n\n- Gifts (published, version 1), predicted, as Ann: 3 rows, then 2;"
        in text
    )
    assert "Not compared: 1 results that are new" in text


def test_compare_between_two_digests_after_the_upgrade_lists_what_changed() -> None:
    previous = _result([_item("kpi", "user:1", now=_outcome("a", 3))], "s1", simulated=False)
    current = _result([_item("kpi", "user:1", now=_outcome("b", 2))], "s1", simulated=False)
    found = results_digest.compare(previous, current)
    assert [(c["item"], c["predicted"]) for c in found["changes"]] == [("kpi", None)]
    assert found["missed"] == []
    text = results_digest.compare_report(previous, current)
    assert "**1 results changed.**" in text and "predicted" not in text
    assert "## Changed\n\n- Gifts (published, version 1), kpi, as Ann: 3 rows, then 2;" in text
    assert "Refreshed since" not in text
