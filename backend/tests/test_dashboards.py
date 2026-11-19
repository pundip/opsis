"""Milestone 4 backend: dashboard specs, versions, publishing, and item data."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal, Role
from opsis.dashboards import spec as specs
from opsis.db.models import AuditLog, Dashboard, DashboardShare
from opsis.errors import ApiError
from opsis.semantic.calendar import CalendarConfig
from opsis.services import dashboards
from tests.test_ingestion import ok
from tests.test_query_api import MODEL_YAML, load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]

SPEC: dict[str, Any] = {
    "specVersion": 1,
    "title": "Gifts overview",
    "theme": {"preset": "default"},
    "filters": [
        {"id": "f_date", "type": "dateRange", "field": "gifts.gift_date",
         "default": {"from": "2025-07-01", "to": "2026-06-30"}},
        {"id": "f_region", "type": "multiSelect", "field": "gifts.region"},
        {"id": "f_tier", "type": "select", "field": "donors.tier", "targets": ["v_by_fund"]},
    ],
    "layout": {"columns": 12, "rowHeight": 40},
    "items": [
        {"id": "v_total", "visual": "kpi", "position": {"x": 0, "y": 0, "w": 3, "h": 3},
         "dataset": "gifts", "bindings": {"value": ["gifts.total_amount"]},
         "config": {"comparison": "previous_period"}},
        {"id": "v_by_fund", "visual": "bar", "position": {"x": 3, "y": 0, "w": 9, "h": 6},
         "dataset": "gifts",
         "bindings": {"category": ["gifts.fund"], "value": ["gifts.total_amount", "gifts.gift_count"]},
         "sort": [{"field": "gifts.total_amount", "dir": "desc"}], "limit": 3,
         "config": {"orientation": "horizontal"}},
        {"id": "v_trend", "visual": "line", "position": {"x": 0, "y": 6, "w": 8, "h": 6},
         "dataset": "gifts",
         "bindings": {"x": [{"field": "gifts.gift_date", "grain": "fiscal_quarter"}],
                      "y": ["gifts.total_amount"], "series": ["donors.tier"]}},
        {"id": "v_table", "visual": "table", "position": {"x": 0, "y": 12, "w": 12, "h": 6},
         "dataset": "gifts",
         "bindings": {"columns": ["gifts.region", "gifts.band", "gifts.gift_count", "gifts.avg_gift"]},
         "segments": ["gifts.large"]},
        {"id": "v_donors", "visual": "pie", "position": {"x": 8, "y": 6, "w": 4, "h": 6},
         "dataset": "donors", "bindings": {"category": ["donors.tier"], "value": ["donors.row_count"]}},
        {"id": "v_note", "visual": "text", "position": {"x": 0, "y": 18, "w": 12, "h": 2},
         "config": {"markdown": "## Notes"}},
    ],
    "interactions": {"crossFilter": True},
}  # fmt: skip


def setup(client: TestClient, source: dict[str, Any]) -> str:  # noqa: F811
    load(client, source)
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    return str(ok(client.post("/api/dashboards", json={"title": "Gifts overview"}), 201)["id"])


def data(client: TestClient, dashboard: str, item: str, **body: Any) -> dict[str, Any]:
    return ok(client.post(f"/api/dashboards/{dashboard}/data", json={"itemId": item, **body}))  # type: ignore[no-any-return]


def problems_for(client: TestClient, spec: Any) -> dict[str, str]:
    result = ok(client.post("/api/dashboards/validate", json={"spec": spec}))
    return {p["path"]: p["message"] for p in result["problems"]}


def changed(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    spec = copy.deepcopy(SPEC)
    change(spec)
    return spec


def test_visual_manifests_are_served(client_for: ClientFor) -> None:
    visuals = {v["id"]: v for v in ok(client_for().get("/api/visuals"))}
    assert set(visuals) == {
        "area",
        "bar",
        "combo",
        "kpi",
        "line",
        "pie",
        "scatter",
        "table",
        "text",
    }
    assert visuals["bar"]["roles"]["value"] == {
        "kind": "measure",
        "min": 1,
        "max": 5,
        "label": "Values",
    }
    assert visuals["text"]["roles"] == {} and all(v["aiHints"] for v in visuals.values())


def test_save_reload_versions_restore_and_publish(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = client_for()
    dashboard = setup(client, source)
    created = ok(client.get(f"/api/dashboards/{dashboard}"))
    assert (
        created["version"] == 1
        and created["publishedVersion"] is None
        and created["spec"]["items"] == []
    )

    saved = ok(
        client.put(
            f"/api/dashboards/{dashboard}",
            json={"spec": SPEC, "baseVersion": 1, "message": "Six visuals"},
        )
    )
    assert saved["version"] == 2 and saved["hasUnpublishedChanges"]
    reloaded = ok(client.get(f"/api/dashboards/{dashboard}"))
    assert reloaded["spec"] == SPEC
    assert list(reloaded["spec"]) == list(SPEC)  # key order survives storage
    assert [i["id"] for i in reloaded["spec"]["items"]] == [i["id"] for i in SPEC["items"]]

    renamed = changed(lambda s: s.update(title="Gifts, renamed"))
    assert (
        ok(client.put(f"/api/dashboards/{dashboard}", json={"spec": renamed, "baseVersion": 2}))[
            "title"
        ]
        == "Gifts, renamed"
    )
    stale = client.put(f"/api/dashboards/{dashboard}", json={"spec": SPEC, "baseVersion": 2})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

    versions = ok(client.get(f"/api/dashboards/{dashboard}/versions"))
    assert [(v["version"], v["message"], v["authorName"]) for v in versions] == [
        (3, None, "Local admin"), (2, "Six visuals", "Local admin"), (1, "Created", "Local admin"),
    ]  # fmt: skip
    assert (
        ok(client.get(f"/api/dashboards/{dashboard}/versions/2"))["spec"]["title"]
        == "Gifts overview"
    )

    restored = ok(client.post(f"/api/dashboards/{dashboard}/versions/2/restore"))
    assert restored["version"] == 4 and restored["spec"] == SPEC
    assert (
        ok(client.get(f"/api/dashboards/{dashboard}/versions"))[0]["message"]
        == "Restored version 2"
    )

    # Published is a pointer to one version; later saves do not move it.
    published = ok(client.post(f"/api/dashboards/{dashboard}/publish", json={"version": 3}))
    assert published["publishedVersion"] == 3 and published["hasUnpublishedChanges"]
    assert ok(client.post(f"/api/dashboards/{dashboard}/publish"))["publishedVersion"] == 4
    ok(client.put(f"/api/dashboards/{dashboard}", json={"spec": renamed, "baseVersion": 4}))
    listed = ok(client.get("/api/dashboards"))[0]
    assert (listed["version"], listed["publishedVersion"], listed["title"]) == (
        5,
        4,
        "Gifts, renamed",
    )
    assert (
        client.post(f"/api/dashboards/{dashboard}/publish", json={"version": 99}).status_code == 404
    )
    assert ok(client.post(f"/api/dashboards/{dashboard}/unpublish"))["publishedVersion"] is None

    actions = db.scalars(
        select(AuditLog.action).where(AuditLog.action.like("dashboard.%")).order_by(AuditLog.id)
    ).all()
    assert actions == [
        "dashboard.created",
        "dashboard.published",
        "dashboard.published",
        "dashboard.unpublished",
    ]
    assert client.delete(f"/api/dashboards/{dashboard}").status_code == 204
    assert client.get(f"/api/dashboards/{dashboard}").status_code == 404


def test_invalid_specs_are_rejected_with_precise_paths(
    client_for: ClientFor, source: dict[str, Any]
) -> None:  # noqa: F811
    client = client_for()
    dashboard = setup(client, source)
    assert problems_for(client, SPEC) == {}

    # Structure (the JSON Schema).
    found = problems_for(client, changed(lambda s: (s.update(specVersion=2), s["items"][0].update(binding={}),
                                                    s["items"][1]["position"].update(w=0))))  # fmt: skip
    assert set(found) == {"/specVersion", "/items/0", "/items/1/position/w"}
    assert "binding" in found["/items/0"]
    assert set(problems_for(client, "not a spec")) == {"/"}

    # Meaning.
    def break_it(s: dict[str, Any]) -> None:
        s["filters"][1]["id"] = "f_date"  # duplicate id
        s["filters"][0]["field"] = "gifts.fund"  # dateRange on text
        s["filters"][2]["targets"] = ["nope"]
        s["items"][0]["visual"] = "gauge"
        s["items"][1]["position"] = {"x": 8, "y": 0, "w": 9, "h": 6}  # off the grid
        s["items"][1]["bindings"] = {
            "category": ["gifts.total_amount"],
            "value": [],
            "colour": ["gifts.fund"],
        }
        s["items"][1]["config"] = {"orientation": "diagonal", "extra": 1}
        s["items"][1]["sort"] = [{"field": "gifts.region"}]
        s["items"][2]["bindings"]["x"] = [{"field": "gifts.fund", "grain": "month"}]
        s["items"][2]["bindings"]["series"] = ["donors.nope"]
        s["items"][3]["dataset"] = "missing"
        s["items"][4]["bindings"]["value"] = ["gifts.total_amount"]  # another dataset's measure
        s["items"][4]["id"] = "v_table"
        s["items"][5]["bindings"] = {"value": ["gifts.total_amount"]}

    found = problems_for(client, changed(break_it))
    assert "already used at /filters/0" in found["/filters/1/id"]
    assert "needs a time dimension" in found["/filters/0/type"]
    assert "no item with the id 'nope'" in found["/filters/2/targets/0"]
    assert (
        "no visual called 'gauge'" in found["/items/0/visual"]
        and "bar, combo" in found["/items/0/visual"]
    )
    assert "ends at column 17" in found["/items/1/position"]
    assert "is a measure, but Category needs a dimension" in found["/items/1/bindings/category/0"]
    assert "Values needs 1 to 5 field(s), but has 0" in found["/items/1/bindings/value"]
    assert "no role called 'colour'" in found["/items/1/bindings/colour"]
    assert "/items/1/config/orientation" in found and "/items/1/config" in found
    assert "bound to this item" in found["/items/1/sort/0/field"]
    assert "Only a time dimension" in found["/items/2/bindings/x/0/grain"]
    assert "'donors.nope' is not a field" in found["/items/2/bindings/series/0"]
    assert "no dataset called 'missing'" in found["/items/3/dataset"]
    assert "not a field that 'donors' can use" in found["/items/4/bindings/value/0"]
    assert "already used at /items/3" in found["/items/4/id"]
    assert "has no data fields" in found["/items/5/bindings"]

    more = problems_for(client, changed(lambda s: (
        s["items"][2]["bindings"].update(x=[{"field": "gifts.gift_date", "grain": "month"}],
                                         series=[{"field": "gifts.gift_date", "grain": "year"}]),
        s["items"][3].update(segments=["gifts.nope"], filters=[{"field": "gifts.nope", "op": "eq", "values": [1]}]),
        s["items"][0].pop("dataset"),
    )))  # fmt: skip
    assert "Only one field" in more["/items/2/bindings"]
    assert "/items/3/segments/0" in more and "/items/3/filters/0/field" in more
    assert "needs a `dataset`" in more["/items/0"]

    # Saving an invalid spec is refused and adds no version.
    refused = client.put(f"/api/dashboards/{dashboard}", json={"spec": changed(break_it)})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "invalid_spec"
    assert len(refused.json()["error"]["details"]) >= 15
    assert ok(client.get(f"/api/dashboards/{dashboard}"))["version"] == 1
    assert (
        client.post("/api/dashboards", json={"title": "x", "spec": changed(break_it)}).status_code
        == 422
    )


def test_item_data_matches_sql_on_the_source(client_for: ClientFor, source: dict[str, Any]) -> None:  # noqa: F811
    client, conn = client_for(), source["conn"]
    dashboard = setup(client, source)
    ok(client.put(f"/api/dashboards/{dashboard}", json={"spec": SPEC}))
    in_range = "gift_date BETWEEN '2025-07-01' AND '2026-06-30'"

    # KPI with the default date range, and the same number of days before it.
    kpi = data(client, dashboard, "v_total")
    total = conn.execute(f"SELECT sum(amount) FROM src.gifts WHERE {in_range}").fetchone()[0]
    before = conn.execute(
        "SELECT sum(amount) FROM src.gifts WHERE gift_date BETWEEN '2024-07-01' AND '2025-06-30'"
    ).fetchone()[0]
    assert kpi["rows"] == [[float(total)]]
    assert kpi["comparison"] == {"mode": "previous_period", "range": {"from": "2024-07-01", "to": "2025-06-30"},
                                 "rows": [[float(before)]]}  # fmt: skip
    assert kpi["rlsBypassed"] is True  # gifts has a rule that does not apply to the admin
    assert kpi["query"]["filters"] == [{"field": "gifts.gift_date", "op": "in_range",
                                        "range": {"from": "2025-07-01", "to": "2026-06-30"}}]  # fmt: skip

    # Bar: sorted and limited, with the viewer's filter values, including one across a join.
    state = {"filters": {"f_region": ["VIC", "NSW"], "f_tier": "major"}}
    bar = data(client, dashboard, "v_by_fund", state=state)
    expected = conn.execute(
        f"""SELECT g.fund, sum(g.amount), count(*) FROM src.gifts g JOIN src.donors d ON d.id = g.donor_id
            WHERE {in_range} AND g.region IN ('VIC','NSW') AND d.tier = 'major'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 3"""
    ).fetchall()
    assert bar["rows"] == [[f, float(t), n] for f, t, n in expected] and bar["truncated"] is True
    assert [c["name"] for c in bar["columns"]] == [
        "gifts.fund",
        "gifts.total_amount",
        "gifts.gift_count",
    ]
    # f_tier targets only the bar, so the KPI ignores it; f_region applies to both.
    kpi_filtered = data(client, dashboard, "v_total", state=state)
    vic_nsw = conn.execute(
        f"SELECT sum(amount) FROM src.gifts WHERE {in_range} AND region IN ('VIC','NSW')"
    ).fetchone()[0]
    assert kpi_filtered["rows"] == [[float(vic_nsw)]]
    # Clearing the date filter (all time) removes the range and so the comparison.
    all_time = data(
        client, dashboard, "v_total", state={"filters": {"f_date": {"relative": "all_time"}}}
    )
    assert all_time["rows"] == [
        [float(conn.execute("SELECT sum(amount) FROM src.gifts").fetchone()[0])]
    ]
    assert "comparison" not in all_time

    # Line: time grain, series from a joined dataset, and drilling to a finer grain.
    line = data(client, dashboard, "v_trend")
    assert [c["name"] for c in line["columns"]] == [
        "donors.tier",
        "gifts.gift_date",
        "gifts.total_amount",
    ]
    assert {row[1] for row in line["rows"]} == {
        "2025-07-01",
        "2025-10-01",
        "2026-01-01",
        "2026-04-01",
    }
    drilled = data(client, dashboard, "v_trend", state={"grains": {"v_trend": "month"}})
    assert (
        len({row[1] for row in drilled["rows"]}) == 12 and drilled["columns"][1]["grain"] == "month"
    )

    # Cross-filter: a selection in one item filters the others, never itself.
    cross = {"cross": [{"field": "gifts.fund", "values": ["Library"], "source": "v_by_fund"}]}
    library = conn.execute(
        f"SELECT sum(amount) FROM src.gifts WHERE {in_range} AND fund = 'Library'"
    ).fetchone()[0]
    assert data(client, dashboard, "v_total", state=cross)["rows"] == [[float(library)]]
    assert len(data(client, dashboard, "v_by_fund", state=cross)["rows"]) == 3
    # A selection on a joined dataset's field reaches items that do not otherwise use that dataset.
    by_tier = {"cross": [{"field": "donors.tier", "values": ["major"], "source": "v_donors"}]}
    major = conn.execute(
        f"SELECT sum(g.amount) FROM src.gifts g JOIN src.donors d ON d.id = g.donor_id WHERE {in_range} AND d.tier = 'major'"
    ).fetchone()[0]
    assert data(client, dashboard, "v_total", state=by_tier)["rows"] == [[float(major)]]
    # The donors pie has no path to a gifts field, so gifts filters do not touch it.
    pie = data(client, dashboard, "v_donors", state={**cross, "filters": {"f_region": ["VIC"]}})
    assert sorted(pie["rows"]) == [["major", 10], ["regular", 30]] and "filters" not in pie["query"]

    # Table with a segment and an expression dimension; the text item has no data.
    table = data(client, dashboard, "v_table")
    large = conn.execute(
        f"SELECT count(*) FROM src.gifts WHERE {in_range} AND amount >= 500"
    ).fetchone()[0]
    assert sum(row[2] for row in table["rows"]) == large and {row[1] for row in table["rows"]} == {
        "large"
    }
    assert data(client, dashboard, "v_note") == {"itemId": "v_note", "empty": True}
    assert (
        client.post(f"/api/dashboards/{dashboard}/data", json={"itemId": "nope"}).status_code == 404
    )


def test_filter_options_inline_preview_and_published_access(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = client_for()
    dashboard = setup(client, source)
    ok(client.put(f"/api/dashboards/{dashboard}", json={"spec": SPEC}))

    def options(filter_id: str, **body: Any) -> dict[str, Any]:
        return ok(
            client.post(
                f"/api/dashboards/{dashboard}/filter-options", json={"filterId": filter_id, **body}
            )
        )  # type: ignore[no-any-return]

    assert options("f_region") == {"values": ["NSW", "QLD", "VIC"], "truncated": False}
    assert options("f_tier")["values"] == ["major", "regular"]
    assert options("f_region", search="ql")["values"] == ["QLD"]
    assert (
        client.post(
            f"/api/dashboards/{dashboard}/filter-options", json={"filterId": "nope"}
        ).status_code
        == 404
    )

    # The editor previews unsaved changes by sending the spec inline.
    unsaved = changed(
        lambda s: s["items"][1].update(
            limit=2, bindings={"category": ["gifts.region"], "value": ["gifts.gift_count"]}
        )
    )
    preview = data(client, dashboard, "v_by_fund", source="inline", spec=unsaved)
    assert len(preview["rows"]) == 2 and preview["columns"][0]["name"] == "gifts.region"
    assert len(data(client, dashboard, "v_by_fund")["rows"]) == 3  # the saved draft is unchanged
    broken = client.post(f"/api/dashboards/{dashboard}/data",
                         json={"itemId": "v_by_fund", "source": "inline", "spec": {"title": "x"}})  # fmt: skip
    assert broken.status_code == 422
    # A binding the compiler cannot resolve comes back as a clear, per-item error.
    unknown = changed(lambda s: s["items"][1]["bindings"].update(value=["gifts.nope"]))
    refused = client.post(
        f"/api/dashboards/{dashboard}/data",
        json={"itemId": "v_by_fund", "source": "inline", "spec": unknown},
    )
    assert (
        refused.status_code == 422
        and refused.json()["error"]["details"][0]["path"] == "/dimensions/1"
    )

    # Viewers only ever see the published version.
    record = db.get(Dashboard, uuid.UUID(dashboard))
    assert record is not None
    settings = client.app.state.settings  # type: ignore[attr-defined]
    runtime = client.app.state.query_runtime  # type: ignore[attr-defined]
    viewer = Principal(
        uuid.uuid4(),
        "v@example.test",
        "Vera",
        Role.VIEWER,
        "session",
        attributes={"regions": ["VIC"]},
    )
    # Not shared with the viewer: invisible, published or not (M6 access lists).
    assert dashboards.list_dashboards(db, viewer) == []
    with pytest.raises(ApiError) as info:
        dashboards.detail(db, record, viewer)
    assert info.value.status == 404
    db.add(DashboardShare(dashboard_id=record.id, grantee="everyone", role="viewer"))
    db.commit()
    # Shared now, but viewers only see a published version and there is none yet.
    assert dashboards.list_dashboards(db, viewer) == []
    with pytest.raises(ApiError) as info:
        dashboards.detail(db, record, viewer)
    assert info.value.status == 404

    ok(client.post(f"/api/dashboards/{dashboard}/publish"))
    ok(
        client.put(
            f"/api/dashboards/{dashboard}",
            json={"spec": changed(lambda s: s.update(title="Draft title"))},
        )
    )
    db.expire_all()
    seen = dashboards.detail(db, record, viewer)
    assert (seen["source"], seen["spec"]["title"], seen["canEdit"]) == (
        "published",
        "Gifts overview",
        False,
    )
    # Viewers see the published title too: the draft does not leak through the list.
    assert [d["title"] for d in dashboards.list_dashboards(db, viewer)] == ["Gifts overview"]
    kwargs: dict[str, Any] = {
        "item_id": "v_table",
        "inline_spec": None,
        "state": {"filters": {"f_date": None}},
    }
    rows = dashboards.item_data(db, settings, runtime, viewer, record, source="draft", **kwargs)
    assert {row[0] for row in rows["rows"]} == {"VIC"}  # RLS applies, and it is the published spec
    assert rows["rlsBypassed"] is False and "query" not in rows
    with pytest.raises(ApiError) as info:
        dashboards.item_data(
            db,
            settings,
            runtime,
            viewer,
            record,
            source="inline",
            item_id="v_table",
            inline_spec=SPEC,
            state=None,
        )
    assert info.value.status == 403
    viewer_options = dashboards.filter_options(
        db,
        settings,
        runtime,
        viewer,
        record,
        filter_id="f_region",
        source="draft",
        inline_spec=None,
        search=None,
    )
    assert viewer_options["values"] == ["VIC"]  # options respect RLS too


def test_query_derivation_rules() -> None:
    from opsis.semantic import model as semantic

    gifts = semantic.parse_model("gifts", {
        "dimensions": {"fund": {"column": "fund", "type": "string"}, "day": {"column": "day", "type": "time"}},
        "measures": {"total": {"agg": "sum", "column": "amount"}},
        "joins": [{"to": "donors", "on": {"donor_id": "id"}}, {"to": "events", "on": {"id": "gift_id"}, "relationship": "one_to_many"}],
    })  # fmt: skip
    donors = semantic.parse_model(
        "donors", {"dimensions": {"tier": {"column": "tier", "type": "string"}}}
    )
    events = semantic.parse_model(
        "events", {"dimensions": {"kind": {"column": "kind", "type": "string"}}}
    )
    models = {"gifts": gifts, "donors": donors, "events": events}
    assert specs.resolve_field(models, "gifts", "donors.tier") is not None
    assert specs.resolve_field(models, "gifts", "events.kind") is None  # fan-out join
    assert specs.resolve_field(models, "donors", "gifts.fund") is None  # no join that way
    assert specs.resolve_field(models, None, "events.kind") is not None

    table = {"roles": {"columns": {"kind": "any", "min": 1, "max": 9}}}
    item = {"id": "t", "visual": "table", "dataset": "gifts",
            "bindings": {"columns": ["gifts.total", "gifts.fund", {"field": "gifts.day", "grain": "week"}, "gifts.fund"]}}  # fmt: skip
    spec = {"filters": [
        {"id": "a", "type": "search", "field": "gifts.fund", "default": "lib"},
        {"id": "b", "type": "select", "field": "events.kind", "default": "gala"},
        {"id": "c", "type": "dateRange", "field": "gifts.day"},
    ], "interactions": {"crossFilter": False}}  # fmt: skip
    query = specs.item_query(
        spec,
        item,
        table,
        models,
        {"cross": [{"field": "gifts.fund", "values": ["x"], "source": "o"}]},
    )
    assert query == {
        "dataset": "gifts",
        "timeDimension": {"field": "gifts.day", "grain": "week"},
        "dimensions": ["gifts.fund"],
        "measures": ["gifts.total"],
        "filters": [{"field": "gifts.fund", "op": "contains", "values": ["lib"]}],
        "limit": 1000,
    }  # the unreachable filter, the empty one and the disabled cross-filter are all left out
    assert specs.item_query(spec, {"id": "n", "visual": "text"}, {"roles": {}}, models) is None

    cal, today = CalendarConfig(fiscal_year_start_month=7), date(2026, 11, 14)
    ranged = {
        "dataset": "gifts",
        "filters": [{"field": "gifts.day", "op": "in_range", "range": {"relative": "this_month"}}],
    }
    previous = specs.comparison_query(ranged, "previous_period", cal, today)
    assert previous is not None and previous[1] == {"from": "2026-10-02", "to": "2026-10-31"}
    year = specs.comparison_query(ranged, "previous_year", cal, today)
    assert year is not None and year[1] == {"from": "2025-11-01", "to": "2025-11-30"}
    leap = {
        "dataset": "g",
        "filters": [
            {"field": "g.d", "op": "in_range", "range": {"from": "2024-02-29", "to": "2024-02-29"}}
        ],
    }
    assert specs.comparison_query(leap, "previous_year", cal, today)[1] == {
        "from": "2023-02-28",
        "to": "2023-02-28",
    }  # type: ignore[index]
    assert specs.comparison_query({"dataset": "g"}, "previous_period", cal, today) is None
    open_ended = {
        "dataset": "g",
        "filters": [{"field": "g.d", "op": "in_range", "range": {"from": "2024-01-01"}}],
    }
    assert specs.comparison_query(open_ended, "previous_period", cal, today) is None
