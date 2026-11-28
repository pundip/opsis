"""Help with relationships (V2_DESIGN 1.3): the key check behind dragging a field onto
another, "Suggest relationships", the built-in date connector and "Generate a date
table". None of them changes the model until someone confirms."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from fastapi.testclient import TestClient

from opsis.connectors.base import ReadOptions, RefreshMode
from opsis.connectors.date import COLUMNS, DateConnector
from tests.test_auth import admin_client, person
from tests.test_ingestion import ok
from tests.test_mcp import Mcp
from tests.test_query_api import load, query
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
JULY = {"weekStart": "monday", "fiscalYearStartMonth": 7, "fiscalYearLabel": "fy_end_year"}
DONORS = {
    "from": {"table": "gifts", "columns": ["donor_id"]},
    "to": {"table": "donors", "columns": ["id"]},
}


def rows_of(connector: DateConnector) -> list[dict[str, Any]]:
    result = connector.read("dates", RefreshMode.FULL, None, ReadOptions(batch_size=3))
    found: list[dict[str, Any]] = []
    for batch in result.batches:
        found.extend(batch.to_pylist())
    return found


def check(
    client: TestClient, dragged: dict[str, Any], target: dict[str, Any], status: int = 200
) -> Any:
    return ok(client.post("/api/model/key-check", json={"from": dragged, "to": target}), status)


def side(table: str, *columns: str) -> dict[str, Any]:
    return {"table": table, "columns": list(columns)}


def model_version(client: TestClient) -> int:
    return int(ok(client.get("/api/model"))["version"])


# ---- the date connector -----------------------------------------------------------


def test_the_date_connector_makes_one_row_a_day_with_the_workspace_calendar() -> None:
    week = DateConnector({"start": "2026-06-28", "end": "2026-07-05", "_calendar": JULY})
    found = rows_of(week)
    assert [r["date"] for r in found] == [date(2026, 6, 28) + timedelta(days=n) for n in range(8)]
    assert list(found[0]) == [c.name for c in COLUMNS]
    sunday, tuesday, saturday = found[0], found[2], found[6]
    assert (tuesday["fiscal_year"], tuesday["fiscal_month"], tuesday["fiscal_quarter"]) == (
        2026,
        12,
        4,
    )
    july = found[3]
    assert (july["date"], july["fiscal_year"], july["fiscal_month"]) == (date(2026, 7, 1), 2027, 1)
    assert (july["fiscal_year_label"], july["quarter_label"], july["year_month"]) == (
        "FY2027", "Q3", "2026-07",
    )  # fmt: skip
    assert (july["month_name"], july["month_short"], july["day_name"]) == (
        "July",
        "Jul",
        "Wednesday",
    )
    assert (sunday["day_of_week"], sunday["is_weekend"], sunday["week_start"]) == (
        7, True, date(2026, 6, 22),
    )  # fmt: skip
    assert (saturday["day_of_week"], saturday["is_weekend"]) == (6, True)
    assert (tuesday["day_of_week"], tuesday["is_weekend"], tuesday["day_of_year"]) == (
        2,
        False,
        181,
    )

    # The workspace's week and financial-year label are followed.
    other = DateConnector(
        {"start": "2026-06-28", "end": "2026-06-28",
         "_calendar": {**JULY, "weekStart": "sunday", "fiscalYearLabel": "year_span"}}
    )  # fmt: skip
    (only,) = rows_of(other)
    assert (only["day_of_week"], only["week_start"], only["fiscal_year_label"]) == (
        1, date(2026, 6, 28), "2025-26",
    )  # fmt: skip
    columns = rows_of(
        DateConnector({"start": "2026-01-01", "end": "2026-01-02"})
    )  # no calendar: Monday weeks and a July year
    assert columns[0]["fiscal_year"] == 2026

    # With no last date it ends with the next financial year, wherever today falls.
    growing = DateConnector({"start": "2025-07-01", "_calendar": JULY})
    growing.today = lambda: date(2026, 11, 28)  # type: ignore[method-assign]
    assert growing.span() == (date(2025, 7, 1), date(2028, 6, 30))
    growing.today = lambda: date(2026, 6, 30)  # type: ignore[method-assign]
    assert growing.span()[1] == date(2027, 6, 30)
    stream = growing.discover()[0]
    assert (stream.name, stream.primary_key, stream.row_count_estimate) == ("dates", ("date",), 730)
    assert "730 days" in growing.test_connection().message

    # Settings that cannot make a table say why.
    for config, message in (
        ({"start": "2026-07-01", "end": "2026-06-30"}, "is before the first date"),
        ({"start": "1800-01-01", "end": "2026-06-30"}, "more than 36,600 days"),
        ({"start": "01/07/2026"}, "must be a date such as"),
    ):
        tested = DateConnector({**config, "_calendar": JULY}).test_connection()
        assert tested.ok is False and message in tested.message


# ---- a field dragged onto another -------------------------------------------------


def test_a_dragged_pair_is_guessed_from_the_keys_and_saves_nothing(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)

    # Dragged from the one side: the sides are swapped, as the keys' uniqueness says.
    guess = check(client, side("donors", "id"), side("gifts", "donor_id"))
    assert guess["swapped"] is True and guess["id"] == "gifts__donors"
    assert guess["relationship"] == {
        "from": {"table": "gifts", "columns": ["donor_id"]},
        "to": {"table": "donors", "columns": ["id"]},
        "cardinality": "many_to_one",
        "crossFilter": "single",
        "securityFilter": "single",
        "active": True,
        "joinType": "left",
    }
    assert guess["keys"]["from"] == {
        "table": "gifts", "columns": ["donor_id"], "rows": 2000, "emptyKeys": 0,
        "distinctKeys": 45, "unique": False,
    }  # fmt: skip
    assert guess["keys"]["to"]["unique"] is True and guess["keys"]["to"]["rows"] == 40
    assert guess["overlap"] == {"sampled": 45, "matched": 40, "share": 0.8889}
    assert "89% of gifts.donor_id values exist in donors.id" in guess["evidence"]
    assert "donors.id is unique" in guess["evidence"]
    assert guess["confidence"] == 0.89 and guess["warnings"] == []

    # Both sides unique: one-to-one, in the direction it was dragged.
    both = check(client, side("gifts", "id"), side("donors", "id"))
    assert both["relationship"]["cardinality"] == "one_to_one" and both["swapped"] is False
    assert "2% of gifts.id values exist in donors.id" in both["evidence"]

    # Neither unique, or keys of different types: said plainly.
    neither = check(client, side("gifts", "region"), side("donors", "region"))
    assert [w["code"] for w in neither["warnings"]] == ["no_unique_side"]
    assert neither["confidence"] <= 0.3
    mixed = check(client, side("gifts", "fund"), side("donors", "id"))
    assert "key_types_differ" in [w["code"] for w in mixed["warnings"]]
    assert mixed["overlap"]["matched"] == 0

    # A second relationship between the same two tables starts inactive.
    assert model_version(client) == 0
    link = {"op": "add", "path": "/relationships/gifts__donors", "value": DONORS}
    ok(client.patch("/api/model", json={"ops": [link], "baseVersion": 0}))
    again = check(client, side("gifts", "id"), side("donors", "id"))
    assert again["id"] == "gifts__donors_2" and again["relationship"]["active"] is False
    assert "two paths to 'donors' as short as each other" in again["inactiveBecause"]
    assert model_version(client) == 1

    # What cannot be a relationship.
    missing = check(client, side("nope", "id"), side("donors", "id"), 404)
    assert missing["error"]["message"] == "There is no table called 'nope'."
    unknown = check(client, side("gifts", "nope"), side("donors", "id"), 422)
    assert unknown["error"]["message"] == "Table 'gifts' has no column called 'nope'."
    same = check(client, side("gifts", "id"), side("gifts", "donor_id"), 422)
    assert "two different tables" in same["error"]["message"]
    uneven = check(client, side("gifts", "id", "fund"), side("donors", "id"), 422)
    assert "as many columns" in uneven["error"]["message"]


# ---- suggestions ------------------------------------------------------------------


def test_suggestions_come_with_evidence_read_as_the_person(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    admin = admin_client(client_for)
    ids = load(admin, source)
    found = ok(admin.post("/api/model/suggestions", json={}))
    assert [p["id"] for p in found["proposals"]] == ["gifts__donors"]
    proposal = found["proposals"][0]
    assert proposal["relationship"]["from"] == DONORS["from"]
    assert proposal["relationship"]["to"] == DONORS["to"]
    assert proposal["confidence"] == 0.89
    assert proposal["evidence"] == [
        "89% of gifts.donor_id values exist in donors.id",
        "donors.id is unique",
        "'donor_id' names the table 'donors' and its column 'id'",
    ]
    assert "Nothing is saved until you confirm" in found["note"]
    assert model_version(admin) == 0
    assert ok(admin.post("/api/model/suggestions", json={"tables": ["donors"]}))["proposals"]
    missing = ok(admin.post("/api/model/suggestions", json={"tables": ["nope"]}), 404)
    assert missing["error"]["message"] == "There is no table called 'nope'."

    # Values are compared as the person sees them: a rule on donors hides most of them.
    rule = {"op": "add", "path": "/tables/donors/rls", "value": [
        {"filter": "region IN {{ user.attributes.regions }}", "applies_to_roles": ["editor"]}]}  # fmt: skip
    ok(admin.patch("/api/model", json={"ops": [rule], "baseVersion": 0}))
    everywhere, _ = person(
        client_for, admin, "all@example.test", "editor", {"regions": ["VIC", "NSW", "QLD"]}
    )
    vic, _ = person(client_for, admin, "vic@example.test", "editor", {"regions": ["VIC"]})
    assert len(ok(everywhere.post("/api/model/suggestions", json={}))["proposals"]) == 1
    assert ok(vic.post("/api/model/suggestions", json={}))["proposals"] == []
    seen = check(vic, side("gifts", "donor_id"), side("donors", "id"))
    assert seen["overlap"] == {"sampled": 45, "matched": 13, "share": 0.2889}
    assert seen["keys"]["to"]["rows"] == 40  # uniqueness counts every row

    # Once related, it is not proposed again.
    link = {"op": "add", "path": "/relationships/gifts__donors", "value": DONORS}
    ok(admin.patch("/api/model", json={"ops": [link], "baseVersion": 1}))
    assert ok(admin.post("/api/model/suggestions", json={}))["proposals"] == []

    # Restricted datasets are left out, and to someone who may not use one it does not exist.
    unlink = {"op": "remove", "path": "/relationships/gifts__donors"}
    ok(admin.patch("/api/model", json={"ops": [unlink], "baseVersion": 2}))
    ok(admin.put(f"/api/datasets/{ids['donors']}/access", json={"restricted": True}))
    assert ok(admin.post("/api/model/suggestions", json={}))["proposals"] == []
    refused = ok(admin.post("/api/model/suggestions", json={"tables": ["donors"]}), 422)
    assert "restricted dataset" in refused["error"]["message"]
    hidden = ok(everywhere.post("/api/model/suggestions", json={"tables": ["donors"]}), 404)
    assert hidden["error"]["message"] == "There is no table called 'donors'."
    hidden = check(everywhere, side("gifts", "donor_id"), side("donors", "id"), 404)
    assert hidden["error"]["message"] == "There is no table called 'donors'."

    # Viewers may not use the Model view's help at all.
    viewer, _ = person(client_for, admin, "viewer@example.test", "viewer")
    assert viewer.post("/api/model/suggestions", json={}).status_code == 403
    assert viewer.post("/api/model/date-table", json={}).status_code == 403


def test_the_agent_gets_suggestions_and_applies_nothing(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    made = client.post("/api/tokens", json={"name": "agent", "scopes": ["read"]})
    agent = Mcp(client, ok(made, 201)["token"])
    found = agent.call("suggest_relationships", tables=["gifts"])
    assert [p["id"] for p in found["proposals"]] == ["gifts__donors"]
    assert "patch_workspace_model" in found["note"] and "Nothing was applied" in found["note"]
    assert "Did you mean 'gifts'?" in agent.fail("suggest_relationships", tables=["gift"])
    assert model_version(client) == 0


# ---- a date table -----------------------------------------------------------------


def test_a_date_table_is_generated_marked_and_offered(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = client_for()
    load(client, source)
    made = ok(client.post("/api/model/date-table", json={}), 201)
    # From the start of the financial year of the earliest gift (2025-01-01) to the end
    # of the next financial year.
    start, end = date.fromisoformat(made["start"]), date.fromisoformat(made["end"])
    assert start == date(2024, 7, 1)
    assert (end.month, end.day) == (6, 30) and end > date.today()
    assert made["rows"] == (end - start).days + 1
    assert made["table"] == "dates" and made["version"] == 1
    assert "nothing is related until you confirm" in made["note"]
    assert "Scheduled refreshes are off on this server" in made["note"]

    # Marked as the date table, refreshed every day.
    model = ok(client.get("/api/model"))
    assert model["document"]["tables"]["dates"]["dateTable"] == {"column": "date"}
    assert model["document"]["relationships"] == {}
    dataset = ok(client.get(f"/api/datasets/{made['dataset']['id']}"))
    assert dataset["label"] == "Dates" and dataset["snapshot"]["rowCount"] == made["rows"]
    table = model["document"]["tables"]["dates"]
    assert table["measures"] == {"days": {"agg": "count", "label": "Days"}}
    assert table["dimensions"]["fiscal_year"] == {
        "column": "fiscal_year", "type": "number", "label": "Financial year",
    }  # fmt: skip
    schedule = ok(client_for(feature_scheduled_refresh=True).get(
        f"/api/datasets/{made['dataset']['id']}"))["schedule"]  # fmt: skip
    assert schedule["cron"] == "5 0 * * *"

    # Relationships from date columns are offered, and work once confirmed.
    (offer,) = made["proposals"]
    assert offer["id"] == "gifts__dates" and offer["relationship"]["active"] is True
    assert offer["relationship"]["to"] == {"table": "dates", "columns": ["date"]}
    assert offer["evidence"][0] == "100% of gifts.gift_date values exist in dates.date"
    assert "'dates' is the date table, and gifts.gift_date is a date" in offer["evidence"]
    confirm = {"op": "add", "path": f"/relationships/{offer['id']}", "value": offer["relationship"]}
    ok(client.patch("/api/model", json={"ops": [confirm], "baseVersion": 1}))
    by_year = query(
        client,
        {"dataset": "gifts", "measures": ["gifts.row_count"], "dimensions": ["dates.fiscal_year"]},
    )
    expected = (
        source["conn"]
        .execute(
            "SELECT CASE WHEN extract(month FROM gift_date) >= 7 THEN extract(year FROM gift_date) + 1 "
            "ELSE extract(year FROM gift_date) END::int AS fy, count(*) FROM src.gifts GROUP BY 1"
        )
        .fetchall()
    )
    assert sorted(map(tuple, by_year["rows"])) == sorted((int(y), n) for y, n in expected)

    # Names are checked; a given first date is kept.
    assert ok(client.post("/api/model/date-table", json={}), 409)["error"]["code"] == "name_taken"
    assert client.post("/api/model/date-table", json={"name": "Dates!"}).status_code == 422
    second = ok(
        client.post("/api/model/date-table", json={"name": "calendar", "start": "2020-01-01"}), 201
    )
    assert (
        second["start"] == "2020-01-01" and second["connection"]["name"] == "Dates (date table) 2"
    )
    # Only the gifts' date is offered: one date table is not related to another.
    assert [p["relationship"]["from"] for p in second["proposals"]] == [
        {"table": "gifts", "columns": ["gift_date"]}
    ]
