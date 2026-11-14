"""Milestone 3.5: timezones, conditional measures, value hardening, model versions, concurrency."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from opsis.errors import ApiError
from opsis.query.duckdb_engine import DuckDBEngine, ResultCache
from opsis.query.engine import CompiledQuery, QueryResult, TableRef
from opsis.semantic import model as semantic
from opsis.semantic.calendar import CalendarConfig
from opsis.semantic.compiler import Source, UserContext, compile_query
from opsis.services.query import QueryRuntime
from tests.test_ingestion import connect, make_dataset, ok, refresh, rows_of, run_jobs, upload
from tests.test_query_api import MODEL_YAML, load, query
from tests.test_query_api import source as source  # noqa: F401  (fixture)
from tests.test_semantic import ADMIN, GIFT_COLUMNS, GIFTS, GIFTS_MODEL, SPEC, write_parquet

ClientFor = Callable[..., TestClient]
ENGINE = DuckDBEngine(memory_limit_mb=256, threads=2)
MELBOURNE = CalendarConfig(fiscal_year_start_month=7, timezone="Australia/Melbourne")
TODAY = date(2026, 11, 14)


def run(
    sources: dict[str, Source],
    q: dict[str, Any],
    user: UserContext = ADMIN,
    calendar: CalendarConfig = MELBOURNE,
) -> list[dict[str, Any]]:
    compiled, _ = compile_query(q, sources, user, calendar, TODAY, max_rows=10_000, timeout_s=20)
    return ENGINE.execute(compiled).table.to_pylist()


# ---- 1. timezones -------------------------------------------------------------------

# (UTC instant, what a Melbourne clock shows)
INSTANTS = [
    (datetime(2026, 1, 5, 20, 0), datetime(2026, 1, 6, 7, 0)),  # summer, UTC+11: next day
    (datetime(2026, 6, 30, 13, 30), datetime(2026, 6, 30, 23, 30)),  # winter, UTC+10
    (datetime(2026, 6, 30, 14, 30), datetime(2026, 7, 1, 0, 30)),  # crosses into the new FY
    (datetime(2026, 10, 3, 15, 59), datetime(2026, 10, 4, 1, 59)),  # just before DST starts
    (datetime(2026, 10, 3, 16, 0), datetime(2026, 10, 4, 3, 0)),  # DST: 2 am does not exist
]


@pytest.fixture(scope="module")
def events(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Source]:
    folder = tmp_path_factory.mktemp("events") / "events"
    folder.mkdir()
    utc = [i for i, _ in INSTANTS]
    table = pa.table(
        {
            "id": list(range(len(utc))),
            "happened": pa.array(
                [d.replace(tzinfo=UTC) for d in utc], type=pa.timestamp("us", tz="UTC")
            ),
            "logged": pa.array(utc, type=pa.timestamp("us")),  # the same clock readings, no zone
            "day": pa.array([d.date() for d in utc]),
        }
    )
    pq.write_table(table, folder / "part-00000.parquet")
    columns = {"id": "integer", "happened": "timestamp", "logged": "timestamp", "day": "date"}
    definition = {
        "dimensions": {
            "happened": {"column": "happened", "type": "time"},
            "logged": {"column": "logged", "type": "time"},
            "logged_utc": {"column": "logged", "type": "time", "timezone": "UTC"},
            "logged_ny": {"column": "logged", "type": "time", "timezone": "America/New_York"},
            "day": {"column": "day", "type": "time", "timezone": "UTC"},
        },
        "measures": {"n": {"agg": "count"}},
    }
    assert semantic.validate(SPEC, "events", definition, columns, {}) == []
    return {
        "events": Source(
            semantic.parse_model("events", definition),
            columns,
            TableRef("events", "d", "s", str(folder)),
            timezones={"happened": "UTC"},
        )
    }


def by_grain(sources: dict[str, Source], field: str, grain: str, **kw: Any) -> list[date]:
    q = {
        "dataset": "events",
        "measures": ["events.n"],
        "timeDimension": {"field": f"events.{field}", "grain": grain},
    }
    return sorted(r[f"events.{field}"] for r in run(sources, q, **kw) for _ in range(r["events.n"]))


def test_instants_are_grouped_by_workspace_days(events: dict[str, Source]) -> None:
    local_days = sorted(local.date() for _, local in INSTANTS)
    utc_days = sorted(utc.date() for utc, _ in INSTANTS)
    assert local_days != utc_days
    # A column that holds instants, and a naive column declared to be UTC, both convert.
    assert by_grain(events, "happened", "day") == local_days
    assert by_grain(events, "logged_utc", "day") == local_days
    # A naive timestamp is workspace-local by default: grouped as written.
    assert by_grain(events, "logged", "day") == utc_days
    # Dates never shift, whatever the model says.
    assert by_grain(events, "day", "day") == utc_days
    # Another source zone: 20:00 in New York on 5 Jan is midday on 6 Jan in Melbourne.
    assert by_grain(events, "logged_ny", "day")[0] == date(2026, 1, 6)
    # With no workspace timezone configured nothing is converted.
    assert by_grain(events, "happened", "day", calendar=CalendarConfig()) == utc_days


def test_grains_and_ranges_use_the_converted_time(events: dict[str, Source]) -> None:
    # 30 June 14:30 UTC is already 1 July in Melbourne: the next financial year.
    assert (
        by_grain(events, "happened", "fiscal_year")
        == [date(2025, 7, 1)] * 2 + [date(2026, 7, 1)] * 3
    )
    assert (
        by_grain(events, "logged", "fiscal_year") == [date(2025, 7, 1)] * 3 + [date(2026, 7, 1)] * 2
    )

    def count(field: str, **range_: str) -> int:
        q = {
            "dataset": "events",
            "measures": ["events.n"],
            "timeDimension": {"field": f"events.{field}", "range": range_},
        }
        return int(run(events, q)[0]["events.n"])

    assert count("happened", **{"from": "2026-07-01", "to": "2026-07-01"}) == 1
    assert count("logged", **{"from": "2026-07-01", "to": "2026-07-01"}) == 0
    assert count("happened", **{"from": "2026-01-06", "to": "2026-01-06"}) == 1
    # Filters see the same converted value, and so does a plain (ungrouped) dimension.
    filtered = {"dataset": "events", "measures": ["events.n"],
                "filters": [{"field": "events.happened", "op": "gte", "values": ["2026-10-04"]}]}  # fmt: skip
    assert run(events, filtered)[0]["events.n"] == 2
    plain = run(
        events,
        {
            "dataset": "events",
            "dimensions": ["events.happened"],
            "sort": [{"field": "events.happened"}],
        },
    )
    assert [r["events.happened"] for r in plain] == [local for _, local in INSTANTS]


def test_timezone_setting_is_validated() -> None:
    columns = {"at": "timestamp", "name": "string"}

    def check(dimension: dict[str, Any]) -> dict[str, str]:
        found = semantic.validate(SPEC, "x", {"dimensions": {"d": dimension}}, columns, {})
        return {p["path"]: p["message"] for p in found}

    assert check({"column": "at", "type": "time", "timezone": "UTC"}) == {}
    assert (
        "not a timezone name"
        in check({"column": "at", "type": "time", "timezone": "Mars/Olympus"})[
            "/dimensions/d/timezone"
        ]
    )
    assert (
        "Only time dimensions"
        in check({"column": "name", "type": "string", "timezone": "UTC"})["/dimensions/d/timezone"]
    )


def test_timezone_aware_sources_are_stored_as_instants(
    client_for: ClientFor, source: dict[str, Any]
) -> None:  # noqa: F811
    conn = source["conn"]
    conn.execute(
        "CREATE TABLE src.events (id integer PRIMARY KEY, happened timestamptz, logged timestamp)"
    )
    conn.execute(
        """INSERT INTO src.events VALUES
           (1, '2026-01-05 20:00+00', '2026-01-05 20:00'),
           (2, '2026-06-30 23:30+10', '2026-06-30 23:30'),
           (3, '2026-07-01 00:30+10', '2026-07-01 00:30')"""
    )
    client = client_for()  # workspace timezone: Australia/Melbourne
    connection = connect(client, "postgres", source["config"])
    dataset = make_dataset(client, connection, "src.events", name="events")
    run_jobs(client)
    detail = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert detail["snapshot"]["columns"] == [
        {"name": "id", "type": "integer"},
        {"name": "happened", "type": "timestamp", "tz": "UTC"},
        {"name": "logged", "type": "timestamp"},
    ]
    stored = rows_of(client, dataset["id"])
    assert stored[1] == {
        "id": 2,
        "happened": "2026-06-30T13:30:00+00:00",
        "logged": "2026-06-30T23:30:00",
    }

    def days(field: str) -> list[str]:
        result = query(client, {"dataset": "events", "measures": ["events.row_count"],
                                "timeDimension": {"field": f"events.{field}", "grain": "day"}})  # fmt: skip
        return [row[0] for row in result["rows"]]

    assert days("happened") == ["2026-01-06", "2026-06-30", "2026-07-01"]
    assert days("logged") == ["2026-01-05", "2026-06-30", "2026-07-01"]
    by_fy = query(client, {"dataset": "events", "measures": ["events.row_count"],
                           "timeDimension": {"field": "events.happened", "grain": "fiscal_year"}})  # fmt: skip
    assert by_fy["rows"] == [["2025-07-01", 2], ["2026-07-01", 1]]

    # A CSV whose timestamps carry offsets is stored the same way.
    csv = b"id,at\n1,2026-01-05T20:00:00+00:00\n2,2026-06-30T23:30:00+10:00\n"
    file_connection = connect(
        client, "file", {"file": upload(client, "at.csv", csv)["file"]}, name="f"
    )
    csv_dataset = make_dataset(client, file_connection, "at", name="at")
    run_jobs(client)
    assert ok(client.get(f"/api/datasets/{csv_dataset['id']}"))["snapshot"]["columns"][1] == {
        "name": "at", "type": "timestamp", "tz": "UTC",
    }  # fmt: skip
    assert [r["at"] for r in rows_of(client, csv_dataset["id"])] == [
        "2026-01-05T20:00:00+00:00",
        "2026-06-30T13:30:00+00:00",
    ]


# ---- 2. conditional measures -----------------------------------------------------------


def gift_sources(
    tmp_path: Path, extra_measures: dict[str, Any], rows: list[dict[str, Any]] = GIFTS
) -> dict[str, Source]:
    definition = {
        **GIFTS_MODEL,
        "measures": {**GIFTS_MODEL["measures"], **extra_measures},
        "joins": [],
        "rls": [],
    }
    assert semantic.validate(SPEC, "gifts", definition, GIFT_COLUMNS, {}) == []
    path = write_parquet(tmp_path / f"gifts{len(list(tmp_path.iterdir()))}", rows, ("amount",))
    return {
        "gifts": Source(
            semantic.parse_model("gifts", definition),
            GIFT_COLUMNS,
            TableRef("gifts", "a", "b", path),
        )
    }


def test_conditional_measures_filter_inside_the_aggregate(tmp_path: Path) -> None:
    sources = gift_sources(
        tmp_path,
        {
            "online_amount": {"agg": "sum", "column": "amount", "filter": "channel = 'online'"},
            "large_count": {"agg": "count", "filter": "{large}"},
            "large_vic_donors": {
                "agg": "count_distinct",
                "column": "donor_id",
                "filter": "{large} AND region = 'VIC'",
            },
            "online_share": {
                "expr": "{online_amount} / NULLIF({total_amount}, 0)",
                "format": "percent",
            },
        },
    )
    q = {
        "dataset": "gifts",
        "dimensions": ["gifts.fund"],
        "measures": ["gifts.total_amount", "gifts.online_amount", "gifts.large_count",
                     "gifts.large_vic_donors", "gifts.online_share"],
    }  # fmt: skip
    rows = run(sources, q, calendar=CalendarConfig())
    assert len(rows) == 4
    for row in rows:
        gifts = [g for g in GIFTS if g["fund"] == row["gifts.fund"]]
        online = sum(g["amount"] for g in gifts if g["channel"] == "online")
        assert row["gifts.total_amount"] == sum(
            g["amount"] for g in gifts
        )  # unconditional, same query
        assert (row["gifts.online_amount"] or 0) == online
        assert row["gifts.large_count"] == sum(g["amount"] >= 500 for g in gifts)
        assert row["gifts.large_vic_donors"] == len(
            {g["donor_id"] for g in gifts if g["amount"] >= 500 and g["region"] == "VIC"}
        )
        assert float(row["gifts.online_share"]) == pytest.approx(
            float(online) / float(row["gifts.total_amount"])
        )
    compiled, _ = compile_query(
        q, sources, ADMIN, CalendarConfig(), TODAY, max_rows=10, timeout_s=5
    )
    assert """SUM("gifts"."amount") FILTER(WHERE "gifts"."channel" = 'online')""" in compiled.sql
    # A conditional measure can be filtered on like any other measure.
    counts = sorted(r["gifts.large_count"] for r in rows)
    having = run(
        sources,
        {**q, "filters": [{"field": "gifts.large_count", "op": "gt", "values": [counts[0]]}]},
        calendar=CalendarConfig(),
    )
    assert len(having) == sum(c > counts[0] for c in counts) and 0 < len(having) < 4


def test_conditional_measure_conditions_use_the_allow_list() -> None:
    def check(measure: dict[str, Any]) -> dict[str, str]:
        definition = {
            "measures": {"m": measure},
            "segments": {"large": {"filter": "amount >= 500"}},
        }
        return {
            p["path"]: p["message"]
            for p in semantic.validate(SPEC, "gifts", definition, GIFT_COLUMNS, {})
        }

    assert check({"agg": "count", "filter": "{large} OR channel IS NULL"}) == {}
    assert (
        "Subqueries"
        in check({"agg": "count", "filter": "amount > (SELECT 1)"})["/measures/m/filter"]
    )
    assert (
        "no segment called 'nope'"
        in check({"agg": "count", "filter": "{nope}"})["/measures/m/filter"]
    )
    assert (
        "no column called 'nope'"
        in check({"agg": "sum", "column": "amount", "filter": "nope = 1"})["/measures/m/filter"]
    )
    assert (
        "Aggregate functions"
        in check({"agg": "count", "filter": "sum(amount) > 1"})["/measures/m/filter"]
    )
    assert (
        "READ_CSV" in check({"agg": "count", "filter": "read_csv('x') = 1"})["/measures/m/filter"]
    )
    # Only aggregations can be conditional; a formula combines measures that already are.
    assert "/measures/m" in check({"expr": "{m}", "filter": "amount > 1"})


# ---- 4. literal values -------------------------------------------------------------------

NASTY = [
    "O'Brien",
    "it''s doubled",
    'say "hi"',
    "back\\slash",
    "trailing backslash\\",
    "\\' OR 1=1 --",
    "x' OR '1'='1",
    "'; DROP TABLE gifts; --",
    "-- comment",
    "/* block */ comment",
    "semi;colon",
    "100% _wild_",
    "tab\tand\nnewline",
    "Zoë Çelik",
    "日本語のファンド",
    "gift 🎁 emoji",
    "‮right-to-left",
    "é combining",
    "$1 ? :name @var",
    "{{ user.email }} {measure}",
    "",
]


def test_every_text_value_is_matched_exactly_and_never_executed(tmp_path: Path) -> None:
    rows = [{**GIFTS[i], "id": i, "fund": text} for i, text in enumerate(NASTY)]
    sources = gift_sources(tmp_path, {}, rows)
    for index, text in enumerate(NASTY):
        for op, values in (("eq", [text]), ("in", ["no such fund", text])):
            q = {"dataset": "gifts", "dimensions": ["gifts.fund", "gifts.amount_value"], "measures": ["gifts.gift_count"],
                 "filters": [{"field": "gifts.fund", "op": op, "values": values}]}  # fmt: skip
            result = run(sources, q, calendar=CalendarConfig())
            assert [r["gifts.fund"] for r in result] == [text], (op, text)
            assert result[0]["gifts.amount_value"] == rows[index]["amount"]
        if text:
            contains = {"dataset": "gifts", "measures": ["gifts.gift_count"],
                        "filters": [{"field": "gifts.fund", "op": "contains", "values": [text]}]}  # fmt: skip
            assert run(sources, contains, calendar=CalendarConfig())[0]["gifts.gift_count"] >= 1
    # The table is still there and complete after all of that.
    assert run(
        sources, {"dataset": "gifts", "measures": ["gifts.gift_count"]}, calendar=CalendarConfig()
    ) == [{"gifts.gift_count": len(NASTY)}]
    # The same strings as a user's RLS attribute select exactly their own row.
    secured = {
        "gifts": Source(
            semantic.parse_model(
                "gifts",
                {
                    **GIFTS_MODEL,
                    "joins": [],
                    "rls": [{"filter": "fund IN {{ user.attributes.funds }}"}],
                },
            ),
            GIFT_COLUMNS,
            sources["gifts"].table,
        )
    }
    by_fund = {"dataset": "gifts", "dimensions": ["gifts.fund"]}
    for text in NASTY:
        expected = [text] if text else []  # an empty attribute denies
        for attribute in ([text], text):
            seen = run(
                secured,
                by_fund,
                UserContext("viewer", attributes={"funds": attribute}),
                CalendarConfig(),
            )
            assert [r["gifts.fund"] for r in seen] == (expected if attribute != [""] else [""]), (
                text,
                attribute,
            )


def test_values_are_checked_against_the_field_type_before_rendering(tmp_path: Path) -> None:
    sources = gift_sources(tmp_path, {})

    def problem(field: str, op: str, values: list[Any]) -> str:
        q = {
            "dataset": "gifts",
            "measures": ["gifts.gift_count"],
            "filters": [{"field": field, "op": op, "values": values}],
        }
        with pytest.raises(ApiError) as info:
            compile_query(q, sources, ADMIN, CalendarConfig(), TODAY, max_rows=10, timeout_s=5)
        return " ".join(f"{d['path']} {d['message']}" for d in info.value.details)

    # Numbers must be real, finite numbers.
    for bad in ["1", "1 OR 1=1", "1e3", True, None, [1], {"a": 1}]:
        assert "Expected a number" in problem("gifts.amount_value", "gt", [bad]), bad
    for bad in [float("nan"), float("inf"), float("-inf")]:
        assert "finite number" in problem("gifts.amount_value", "gt", [bad])
    assert "too large" in problem("gifts.amount_value", "eq", [10**30])
    assert "/filters/0/values/1" in problem("gifts.amount_value", "in", [1, "2"])
    assert "Expected a number" in problem("gifts.total_amount", "gt", ["1; DROP TABLE x"])
    # Dates must parse as dates.
    for bad in ["today", "2026-13-01", "2026-01-01' OR '1'='1", "2026-01-01; DROP", 20260101, None]:
        assert "Expected a date" in problem("gifts.gift_date", "gte", [bad]), bad
    # Text is length-capped and may not contain a null byte.
    assert "longer than 1000" in problem("gifts.fund", "eq", ["x" * 1001])
    assert "null character" in problem("gifts.fund", "eq", ["a\x00b"])
    assert "null character" in problem("gifts.fund", "contains", ["\x00"])
    assert "finite number" in problem("gifts.fund", "eq", [float("nan")])

    # What does render is exactly what was sent, as a typed literal.
    q = {"dataset": "gifts", "measures": ["gifts.gift_count"], "filters": [
        {"field": "gifts.amount_value", "op": "between", "values": [-1.5, 2e3]},
        {"field": "gifts.gift_date", "op": "lt", "values": ["2026-02-28"]},
        {"field": "gifts.fund", "op": "neq", "values": [42]},
    ]}  # fmt: skip
    sql = compile_query(q, sources, ADMIN, CalendarConfig(), TODAY, max_rows=10, timeout_s=5)[0].sql
    assert '"gifts"."amount" BETWEEN -1.5 AND 2000.0' in sql
    assert """"gifts"."gift_date" < CAST('2026-02-28' AS DATE)""" in sql
    assert """"gifts"."fund" <> '42'""" in sql

    # RLS attributes that cannot be safe literals deny instead of rendering.
    secured = {"gifts": Source(
        semantic.parse_model("gifts", {**GIFTS_MODEL, "joins": [], "rls": [{"filter": "fund = {{ user.attributes.f }}"}]}),
        GIFT_COLUMNS, sources["gifts"].table)}  # fmt: skip
    count = {"dataset": "gifts", "measures": ["gifts.gift_count"]}
    for bad in ["a\x00b", "x" * 1001, float("nan"), float("inf"), 10**30, {"a": 1}, [["x"]]]:
        assert run(
            secured, count, UserContext("viewer", attributes={"f": bad}), CalendarConfig()
        ) == [{"gifts.gift_count": 0}]


def test_api_rejects_oversized_and_malformed_values(
    client_for: ClientFor, source: dict[str, Any]
) -> None:  # noqa: F811
    client = client_for()
    load(client, source)

    def status(values: list[Any], field: str = "gifts.fund") -> int:
        body = {
            "dataset": "gifts",
            "measures": ["gifts.row_count"],
            "filters": [{"field": field, "op": "eq", "values": values}],
        }
        return client.post("/api/query", json={"query": body}).status_code

    assert status(["Library"]) == 200
    assert status(["x" * 1001]) == 422
    assert status(["a\u0000b"]) == 422
    assert status([{"$ne": 1}]) == 422
    assert status([None]) == 422
    assert status(["Library' OR '1'='1"]) == 200  # a string like any other: matches nothing
    assert query(client, {"dataset": "gifts", "measures": ["gifts.row_count"],
                          "filters": [{"field": "gifts.fund", "op": "eq", "values": ["Library' OR '1'='1"]}]})["rows"] == [[0]]  # fmt: skip
    raw = client.post(
        "/api/query",
        content=b'{"query": {"dataset": "gifts", "measures": ["gifts.row_count"], "filters": [{"field": "gifts.amount", "op": "gt", "values": [NaN]}]}}',
        headers={"Content-Type": "application/json"},
    )
    assert raw.status_code == 422


# ---- 3. model version history --------------------------------------------------------------


def test_model_versions_record_every_save_and_can_be_restored(
    client_for: ClientFor, source: dict[str, Any]
) -> None:  # noqa: F811
    client = client_for()
    ids = load(client, source)

    first = ok(client.get("/api/models/gifts"))
    assert first["version"] == 1 and first["isStarter"]
    assert [
        (v["version"], v["authorType"], v["authorName"], v["current"])
        for v in ok(client.get("/api/models/gifts/versions"))
    ] == [(1, "system", "System", True)]
    # Refreshing without a column change does not add a version; a column change does.
    refresh(client, ids["gifts"])
    assert len(ok(client.get("/api/models/gifts/versions"))) == 1
    source["conn"].execute("ALTER TABLE src.gifts ADD COLUMN channel text")
    refresh(client, ids["gifts"], full=True)
    regenerated = ok(client.get("/api/models/gifts/versions"))
    assert [v["version"] for v in regenerated] == [2, 1] and "columns changed" in regenerated[0][
        "message"
    ]

    saved = ok(
        client.put(
            "/api/models/gifts",
            json={"yaml": MODEL_YAML, "baseVersion": 2, "message": "Add measures and RLS"},
        )
    )
    assert saved["version"] == 3 and not saved["isStarter"]
    edited = MODEL_YAML.replace("label: Gifts", "label: Gifts and pledges")
    assert (
        ok(client.put("/api/models/gifts", json={"yaml": edited, "baseVersion": 3}))["version"] == 4
    )

    versions = ok(client.get("/api/models/gifts/versions"))
    assert [
        (v["version"], v["authorType"], v["authorName"], v["message"]) for v in versions[:2]
    ] == [
        (4, "user", "Local admin", None),
        (3, "user", "Local admin", "Add measures and RLS"),
    ]
    assert [v["current"] for v in versions] == [True, False, False, False]
    old = ok(client.get("/api/models/gifts/versions/3"))
    assert old["definition"]["label"] == "Gifts" and "label: Gifts\n" in old["yaml"]
    assert list(old["definition"]["measures"]) == [
        "total_amount",
        "gift_count",
        "donors",
        "avg_gift",
    ]  # order kept
    assert client.get("/api/models/gifts/versions/99").status_code == 404

    # A save based on a stale version is refused, and nothing changes.
    stale = client.put("/api/models/gifts", json={"yaml": MODEL_YAML, "baseVersion": 3})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert "now version 4" in stale.json()["error"]["message"]
    assert ok(client.get("/api/models/gifts"))["version"] == 4
    # An invalid save adds no version either.
    assert (
        client.put(
            "/api/models/gifts", json={"yaml": "dimensions: {x: {column: nope, type: string}}"}
        ).status_code
        == 422
    )
    assert len(ok(client.get("/api/models/gifts/versions"))) == 4

    # Restore makes a new version with the old content; history is never rewritten.
    restored = ok(client.post("/api/models/gifts/versions/3/restore"))
    assert restored["version"] == 5 and restored["definition"]["label"] == "Gifts"
    latest = ok(client.get("/api/models/gifts/versions"))[0]
    assert (latest["version"], latest["message"]) == (5, "Restored version 3")
    assert (
        ok(client.get("/api/models/gifts/versions/4"))["definition"]["label"] == "Gifts and pledges"
    )
    # Restoring a version that no longer fits the data is refused with the reason.
    source["conn"].execute("ALTER TABLE src.gifts RENAME COLUMN fund TO fund_name")
    refresh(client, ids["gifts"], full=True)
    broken = client.post("/api/models/gifts/versions/3/restore")
    assert broken.status_code == 422
    assert any(d["path"] == "/dimensions/fund/column" for d in broken.json()["error"]["details"])
    # An edited model is not regenerated by the refresh, so there is still no version 6.
    assert ok(client.get("/api/models/gifts"))["version"] == 5


# ---- 5. concurrency and RLS exemption --------------------------------------------------------


class SlowEngine:
    dialect = "duckdb"

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()

    def execute(self, q: CompiledQuery) -> QueryResult:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
        return QueryResult(pa.table({"x": [1]}), 1, False, 1.0)


def test_only_a_limited_number_of_queries_run_at_once() -> None:
    engine = SlowEngine(0.15)
    runtime = QueryRuntime(engine=engine, cache=ResultCache(0), max_concurrent=2)  # type: ignore[arg-type]
    errors: list[Exception] = []

    def call(n: int, wait: float) -> None:
        try:
            runtime.execute(CompiledQuery(f"SELECT {n}", (), (), 10, 5), wait_seconds=wait)
        except Exception as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(n, 5)) for n in range(8)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and engine.peak == 2
    assert time.monotonic() - started >= 0.15 * 4 * 0.9  # eight queries, two at a time

    # When the wait is longer than a caller will tolerate, it is told the server is busy.
    slow = SlowEngine(0.5)
    busy = QueryRuntime(engine=slow, cache=ResultCache(0), max_concurrent=1)  # type: ignore[arg-type]
    holder = threading.Thread(
        target=lambda: busy.execute(CompiledQuery("SELECT 1", (), (), 10, 5), wait_seconds=5)
    )
    holder.start()
    time.sleep(0.1)
    with pytest.raises(ApiError) as info:
        busy.execute(CompiledQuery("SELECT 2", (), (), 10, 5), wait_seconds=0.05)
    holder.join()
    assert info.value.status == 503 and info.value.code == "busy"
    # A cached result needs no slot at all.
    cached = QueryRuntime(engine=SlowEngine(0), cache=ResultCache(10), max_concurrent=1)  # type: ignore[arg-type]
    key = CompiledQuery("SELECT 3", (), (), 10, 5)
    assert not cached.execute(key, 1).cache_hit and cached.execute(key, 1).cache_hit


def test_rls_exemptions_are_reported_and_never_apply_to_anonymous_access(tmp_path: Path) -> None:
    path = write_parquet(tmp_path / "gifts", GIFTS, ("amount",))
    model = semantic.parse_model(
        "gifts", {**GIFTS_MODEL, "joins": []}
    )  # one rule, for viewers and editors
    sources = {"gifts": Source(model, GIFT_COLUMNS, TableRef("gifts", "a", "b", path))}
    open_model = semantic.parse_model("gifts", {**GIFTS_MODEL, "joins": [], "rls": []})
    count = {"dataset": "gifts", "measures": ["gifts.gift_count"]}

    def compiled(user: UserContext, src: dict[str, Source] = sources) -> CompiledQuery:
        return compile_query(count, src, user, CalendarConfig(), TODAY, max_rows=10, timeout_s=5)[0]

    assert compiled(ADMIN).rls_bypassed is True  # exempt, and it says so
    assert compiled(UserContext("viewer", attributes={"regions": ["VIC"]})).rls_bypassed is False
    assert (
        compiled(
            ADMIN, {"gifts": Source(open_model, GIFT_COLUMNS, sources["gifts"].table)}
        ).rls_bypassed
        is False
    )

    # A share link or anonymous viewer is never exempt, whatever role it carries.
    anonymous_admin = UserContext("admin", may_be_exempt=False)
    assert compiled(anonymous_admin).rls_bypassed is False
    assert ENGINE.execute(compiled(anonymous_admin)).table.to_pylist() == [{"gifts.gift_count": 0}]
    with_attributes = UserContext("admin", attributes={"regions": ["NSW"]}, may_be_exempt=False)
    assert ENGINE.execute(compiled(with_attributes)).table.to_pylist() == [
        {"gifts.gift_count": sum(g["region"] == "NSW" for g in GIFTS)}
    ]
