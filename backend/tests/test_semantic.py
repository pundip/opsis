"""Milestone 3: the semantic model, the expression allow-list and the query compiler.

These tests build small Parquet snapshots by hand, compile semantic queries
against them and compare the rows with results computed in plain Python.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlglot import exp

from opsis.errors import ApiError
from opsis.query.duckdb_engine import DuckDBEngine, ResultCache
from opsis.query.engine import CompiledQuery, QueryResult, QueryTimeoutError, TableRef
from opsis.semantic import expressions
from opsis.semantic import model as semantic
from opsis.semantic.calendar import CalendarConfig, period_start
from opsis.semantic.compiler import Source, UserContext, compile_query, grain_expression
from opsis.semantic.expressions import ExpressionError

SPEC = Path("/app/spec")
TODAY = date(2026, 11, 14)
AU = CalendarConfig(week_start="monday", fiscal_year_start_month=7)
ADMIN = UserContext("admin", "admin@example.test")
FUNDS = ["Library", "Sport", "Scholarships", "Medical Research"]
REGIONS = ["VIC", "NSW", "QLD"]

DONORS = [
    {"id": i, "name": name, "region": REGIONS[i % 3], "tier": "major" if i % 4 == 0 else "regular"}
    for i, name in enumerate(["Ana", "Ben", "O'Brien", "Di %_", "Ed", "Flo", "Gus", "Hal"], start=1)
]
GIFTS = [
    {
        "id": n,
        "donor_id": 1 + n % 8 if n % 37 else 99,  # donor 99 does not exist
        "fund": FUNDS[n % 4],
        "amount": Decimal(10 + (n * 37) % 990) + Decimal(n % 100) / 100,
        "gift_date": date(2025, 1, 1) + timedelta(days=(n * 5) % 700),
        "region": REGIONS[n % 3],
        "channel": None if n % 9 == 0 else ["online", "mail", "event"][n % 3],
        "created_at": datetime(2025, 1, 1, 8) + timedelta(hours=n * 41),
    }
    for n in range(1, 401)
]
GIFT_COLUMNS = {
    "id": "integer", "donor_id": "integer", "fund": "string", "amount": "decimal",
    "gift_date": "date", "region": "string", "channel": "string", "created_at": "timestamp",
}  # fmt: skip
DONOR_COLUMNS = {"id": "integer", "name": "string", "region": "string", "tier": "string"}

GIFTS_MODEL: dict[str, Any] = {
    "dimensions": {
        "fund": {"column": "fund", "type": "string"},
        "region": {"column": "region", "type": "string"},
        "channel": {"column": "channel", "type": "string"},
        "gift_date": {"column": "gift_date", "type": "time"},
        "created_at": {"column": "created_at", "type": "time", "grains": ["day", "month"]},
        "size": {"expr": "CASE WHEN amount >= 500 THEN 'large' ELSE 'small' END", "type": "string"},
        "amount_value": {"column": "amount", "type": "number"},
    },
    "measures": {
        "total_amount": {"agg": "sum", "column": "amount", "format": "currency_aud"},
        "gift_count": {"agg": "count"},
        "donors": {"agg": "count_distinct", "column": "donor_id"},
        "avg_amount": {"agg": "avg", "column": "amount"},
        "smallest": {"agg": "min", "column": "amount"},
        "largest": {"agg": "max", "column": "amount"},
        "with_channel": {"agg": "count", "column": "channel"},
        "avg_gift": {"expr": "{total_amount} / NULLIF({gift_count}, 0)"},
        "avg_gift_rounded": {"expr": "round({avg_gift}, 0)"},
    },
    "segments": {"large": {"filter": "amount >= 500"}, "online": {"filter": "channel = 'online'"}},
    "joins": [{"to": "donors", "on": {"donor_id": "id"}, "relationship": "many_to_one"}],
    "rls": [{"filter": "region IN {{ user.attributes.regions }}"}],
}
DONORS_MODEL: dict[str, Any] = {
    "dimensions": {
        "name": {"column": "name", "type": "string"},
        "tier": {"column": "tier", "type": "string"},
        "region": {"column": "region", "type": "string"},
    },
    "measures": {"donor_count": {"agg": "count"}},
    "joins": [{"to": "gifts", "on": {"id": "donor_id"}, "relationship": "one_to_many"}],
}


def write_parquet(folder: Path, rows: list[dict[str, Any]], decimals: tuple[str, ...] = ()) -> str:
    folder.mkdir(parents=True)
    table = pa.Table.from_pylist(rows)
    for name in decimals:
        index = table.schema.get_field_index(name)
        table = table.set_column(index, name, table[name].cast(pa.decimal128(12, 2)))
    pq.write_table(table, folder / "part-00000.parquet")
    return str(folder)


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Source]:
    root = tmp_path_factory.mktemp("snapshots")
    others = {"gifts": GIFT_COLUMNS, "donors": DONOR_COLUMNS}
    for name, definition, columns in (
        ("gifts", GIFTS_MODEL, GIFT_COLUMNS),
        ("donors", DONORS_MODEL, DONOR_COLUMNS),
    ):
        rest = {k: v for k, v in others.items() if k != name}
        assert semantic.validate(SPEC, name, definition, columns, rest) == []
    return {
        "gifts": Source(
            semantic.parse_model("gifts", GIFTS_MODEL),
            GIFT_COLUMNS,
            TableRef("gifts", "d1", "s1", write_parquet(root / "gifts", GIFTS, ("amount",))),
        ),
        "donors": Source(
            semantic.parse_model("donors", DONORS_MODEL),
            DONOR_COLUMNS,
            TableRef("donors", "d2", "s2", write_parquet(root / "donors", DONORS)),
        ),
    }


ENGINE = DuckDBEngine(memory_limit_mb=256, threads=2)


def compiled(
    sources: dict[str, Source], query: dict[str, Any], user: UserContext = ADMIN, **kw: Any
) -> CompiledQuery:
    kw.setdefault("max_rows", 10_000)
    kw.setdefault("timeout_s", 20)
    kw.setdefault("calendar", AU)
    kw.setdefault("today", TODAY)
    calendar, today = kw.pop("calendar"), kw.pop("today")
    return compile_query(query, sources, user, calendar, today, **kw)[0]


def run(
    sources: dict[str, Source], query: dict[str, Any], user: UserContext = ADMIN, **kw: Any
) -> list[dict[str, Any]]:
    return ENGINE.execute(compiled(sources, query, user, **kw)).table.to_pylist()


def problems(
    sources: dict[str, Source], query: dict[str, Any], user: UserContext = ADMIN
) -> dict[str, str]:
    with pytest.raises(ApiError) as info:
        compiled(sources, query, user)
    assert info.value.status == 422 and info.value.code == "invalid_query"
    return {d["path"]: d["message"] for d in info.value.details}


def by(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row[key]: row for row in rows}


# ---- aggregation --------------------------------------------------------------------


def test_measures_by_dimension_match_plain_python(sources: dict[str, Source]) -> None:
    rows = run(
        sources,
        {
            "dataset": "gifts",
            "dimensions": ["gifts.fund"],
            "measures": [
                "gifts.total_amount", "gifts.gift_count", "gifts.donors", "gifts.avg_amount",
                "gifts.smallest", "gifts.largest", "gifts.with_channel", "gifts.avg_gift",
                "gifts.avg_gift_rounded",
            ],
            "sort": [{"field": "gifts.total_amount", "dir": "desc"}],
        },
    )  # fmt: skip
    expected: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gift in GIFTS:
        expected[gift["fund"]].append(gift)
    assert len(rows) == 4
    assert [r["gifts.total_amount"] for r in rows] == sorted(
        (sum(g["amount"] for g in gs) for gs in expected.values()), reverse=True
    )
    for row in rows:
        gifts = expected[row["gifts.fund"]]
        total = sum(g["amount"] for g in gifts)
        assert row["gifts.total_amount"] == total
        assert row["gifts.gift_count"] == len(gifts)
        assert row["gifts.donors"] == len({g["donor_id"] for g in gifts})
        assert row["gifts.avg_amount"] == pytest.approx(float(total) / len(gifts))
        assert row["gifts.smallest"] == min(g["amount"] for g in gifts)
        assert row["gifts.largest"] == max(g["amount"] for g in gifts)
        assert row["gifts.with_channel"] == sum(g["channel"] is not None for g in gifts)
        assert float(row["gifts.avg_gift"]) == pytest.approx(float(total) / len(gifts))
        assert float(row["gifts.avg_gift_rounded"]) == round(float(total) / len(gifts))


def test_no_dimensions_gives_one_row_and_no_measures_gives_distinct_values(
    sources: dict[str, Source],
) -> None:
    assert run(sources, {"dataset": "gifts", "measures": ["gifts.gift_count"]}) == [
        {"gifts.gift_count": 400}
    ]
    rows = run(sources, {"dataset": "gifts", "dimensions": ["gifts.region", "gifts.size"]})
    assert sorted((r["gifts.region"], r["gifts.size"]) for r in rows) == sorted(
        {(g["region"], "large" if g["amount"] >= 500 else "small") for g in GIFTS}
    )


def test_result_columns_describe_roles_types_and_formats(sources: dict[str, Source]) -> None:
    _, columns = compile_query(
        {
            "dataset": "gifts",
            "dimensions": ["gifts.fund", "donors.tier"],
            "measures": ["gifts.total_amount", "gifts.gift_count", "gifts.avg_gift"],
            "timeDimension": {"field": "gifts.gift_date", "grain": "fiscal_quarter"},
        },
        sources, ADMIN, AU, TODAY, max_rows=100, timeout_s=5,
    )  # fmt: skip
    assert [(c.name, c.role, c.type.value, c.format, c.grain, c.label) for c in columns] == [
        ("gifts.fund", "dimension", "string", None, None, "Fund"),
        ("donors.tier", "dimension", "string", None, None, "Tier"),
        ("gifts.gift_date", "time", "date", None, "fiscal_quarter", "Gift date"),
        ("gifts.total_amount", "measure", "decimal", "currency_aud", None, "Total amount"),
        ("gifts.gift_count", "measure", "integer", None, None, "Gift count"),
        ("gifts.avg_gift", "measure", "float", None, None, "Avg gift"),
    ]


# ---- time ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grain",
    ["day", "week", "month", "quarter", "year", "fiscal_month", "fiscal_quarter", "fiscal_year"],
)
def test_time_grains_match_the_calendar_rules(sources: dict[str, Source], grain: str) -> None:
    rows = run(
        sources,
        {
            "dataset": "gifts",
            "measures": ["gifts.total_amount"],
            "timeDimension": {"field": "gifts.gift_date", "grain": grain},
        },
    )
    expected: dict[date, Decimal] = defaultdict(Decimal)
    for gift in GIFTS:
        expected[period_start(gift["gift_date"], grain, AU)] += gift["amount"]  # type: ignore[arg-type]
    assert {r["gifts.gift_date"]: r["gifts.total_amount"] for r in rows} == dict(expected)
    assert [r["gifts.gift_date"] for r in rows] == sorted(expected)  # oldest first by default


def test_grain_sql_agrees_with_calendar_for_every_setting(tmp_path: Path) -> None:
    """The SQL date truncation and opsis.semantic.calendar must never drift apart."""
    days = [date(2023, 12, 25) + timedelta(days=n) for n in range(0, 900)]
    stamps = [datetime(d.year, d.month, d.day, 23, 59, 59) for d in days]
    pq.write_table(pa.table({"d": days, "ts": stamps}), tmp_path / "dates.parquet")
    conn = duckdb.connect()
    grains = [
        "day",
        "week",
        "month",
        "quarter",
        "year",
        "fiscal_month",
        "fiscal_quarter",
        "fiscal_year",
    ]
    for week_start in ("monday", "sunday"):
        for month in (1, 2, 4, 7, 10, 12):
            config = CalendarConfig(week_start=week_start, fiscal_year_start_month=month)  # type: ignore[arg-type]
            for column in ("d", "ts"):
                parts = [
                    grain_expression(exp.column(column, quoted=True), g, config).sql(
                        dialect="duckdb"
                    )
                    for g in grains
                ]
                rows = conn.execute(
                    f"SELECT d, {', '.join(parts)} FROM read_parquet('{tmp_path}/dates.parquet')"
                ).fetchall()
                for day, *results in rows:
                    for grain, result in zip(grains, results, strict=True):
                        assert result == period_start(day, grain, config), (
                            week_start,
                            month,
                            grain,
                            day,
                        )  # type: ignore[arg-type]


def test_relative_and_absolute_ranges(sources: dict[str, Source]) -> None:
    def count(time_dimension: dict[str, Any], **kw: Any) -> int:
        query = {
            "dataset": "gifts",
            "measures": ["gifts.gift_count"],
            "timeDimension": time_dimension,
        }
        return int(run(sources, query, **kw)[0]["gifts.gift_count"])

    def expect(start: date, end: date) -> int:
        return sum(start <= g["gift_date"] <= end for g in GIFTS)

    field = "gifts.gift_date"
    assert count({"field": field, "range": {"relative": "last_fiscal_year"}}) == expect(
        date(2025, 7, 1), date(2026, 6, 30)
    )
    assert count({"field": field, "range": {"relative": "this_fiscal_year"}}) == expect(
        date(2026, 7, 1), date(2027, 6, 30)
    )
    assert count({"field": field, "range": {"relative": "last_12_months"}}) == expect(
        date(2025, 12, 1), TODAY
    )
    assert count({"field": field, "range": {"from": "2025-03-01", "to": "2025-03-31"}}) == expect(
        date(2025, 3, 1), date(2025, 3, 31)
    )
    assert count({"field": field, "range": {"from": "2026-06-30"}}) == expect(
        date(2026, 6, 30), date.max
    )
    assert count({"field": field, "range": {"relative": "all_time"}}) == 400
    # A different fiscal year start moves the boundaries.
    april = CalendarConfig(fiscal_year_start_month=4)
    assert count(
        {"field": field, "range": {"relative": "last_fiscal_year"}}, calendar=april
    ) == expect(date(2025, 4, 1), date(2026, 3, 31))
    # Timestamps: the end date is inclusive of the whole day.
    stamp = {"field": "gifts.created_at", "range": {"from": "2025-02-01", "to": "2025-02-01"}}
    assert count(stamp) == sum(g["created_at"].date() == date(2025, 2, 1) for g in GIFTS)

    sql = compiled(
        sources,
        {"dataset": "gifts", "measures": ["gifts.gift_count"],
         "timeDimension": {"field": field, "range": {"relative": "this_month"}}},
    ).sql  # fmt: skip
    assert "CAST('2026-11-01' AS DATE)" in sql and "CAST('2026-12-01' AS DATE)" in sql


def test_time_dimension_problems(sources: dict[str, Source]) -> None:
    base = {"dataset": "gifts", "measures": ["gifts.gift_count"]}
    assert "/timeDimension/field" in problems(
        sources, {**base, "timeDimension": {"field": "gifts.fund", "grain": "month"}}
    )
    grain = problems(
        sources, {**base, "timeDimension": {"field": "gifts.created_at", "grain": "year"}}
    )
    assert grain == {"/timeDimension/grain": "'gifts.created_at' allows these grains: day, month."}
    backwards = {"field": "gifts.gift_date", "range": {"from": "2026-02-01", "to": "2026-01-01"}}
    assert "/timeDimension/range" in problems(sources, {**base, "timeDimension": backwards})
    twice = {
        **base,
        "dimensions": ["gifts.gift_date"],
        "timeDimension": {"field": "gifts.gift_date", "grain": "month"},
    }
    assert "selected more than once" in problems(sources, twice)["/"]


# ---- filters, segments, sort, limit ---------------------------------------------------


def test_filter_operators(sources: dict[str, Source]) -> None:
    def count(*filters: dict[str, Any]) -> int:
        query = {"dataset": "gifts", "measures": ["gifts.gift_count"], "filters": list(filters)}
        return int(run(sources, query)[0]["gifts.gift_count"])

    def expect(test: Any) -> int:
        return sum(1 for g in GIFTS if test(g))

    def f(field: str, op: str, *values: Any, **extra: Any) -> dict[str, Any]:
        return {"field": field, "op": op, **({"values": list(values)} if values else {}), **extra}

    assert count(f("gifts.fund", "eq", "Library")) == expect(lambda g: g["fund"] == "Library")
    assert count(f("gifts.fund", "neq", "Library")) == expect(lambda g: g["fund"] != "Library")
    assert count(f("gifts.region", "in", "VIC", "NSW")) == expect(
        lambda g: g["region"] in ("VIC", "NSW")
    )
    assert count(f("gifts.region", "not_in", "VIC", "NSW")) == expect(
        lambda g: g["region"] == "QLD"
    )
    assert count(f("gifts.amount_value", "gt", 500)) == expect(lambda g: g["amount"] > 500)
    assert count(f("gifts.amount_value", "lte", 99.5)) == expect(
        lambda g: g["amount"] <= Decimal("99.5")
    )
    assert count(f("gifts.amount_value", "between", 100, 200)) == expect(
        lambda g: 100 <= g["amount"] <= 200
    )
    assert count(f("gifts.channel", "is_null")) == expect(lambda g: g["channel"] is None)
    assert count(f("gifts.channel", "is_not_null")) == expect(lambda g: g["channel"] is not None)
    assert count(f("gifts.fund", "contains", "SCH")) == expect(lambda g: "sch" in g["fund"].lower())
    assert count(f("gifts.fund", "starts_with", "med")) == expect(
        lambda g: g["fund"] == "Medical Research"
    )
    assert count(f("gifts.fund", "ends_with", "PORT")) == expect(lambda g: g["fund"] == "Sport")
    assert count(f("gifts.gift_date", "gte", "2026-01-01")) == expect(
        lambda g: g["gift_date"] >= date(2026, 1, 1)
    )
    assert count(
        f("gifts.gift_date", "in_range", range={"relative": "last_fiscal_year"})
    ) == expect(lambda g: date(2025, 7, 1) <= g["gift_date"] <= date(2026, 6, 30))
    assert count(f("gifts.size", "eq", "large")) == expect(lambda g: g["amount"] >= 500)
    # Filters are ANDed, and a filter on a joined dimension works.
    assert count(f("gifts.fund", "eq", "Sport"), f("gifts.region", "eq", "VIC")) == expect(
        lambda g: g["fund"] == "Sport" and g["region"] == "VIC"
    )
    majors = {d["id"] for d in DONORS if d["tier"] == "major"}
    assert count(f("donors.tier", "eq", "major")) == expect(lambda g: g["donor_id"] in majors)


def test_measure_filters_become_having(sources: dict[str, Source]) -> None:
    query = {
        "dataset": "gifts",
        "dimensions": ["gifts.fund", "gifts.region"],
        "measures": ["gifts.total_amount"],
        "filters": [{"field": "gifts.gift_count", "op": "gte", "values": [34]}],
    }
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for gift in GIFTS:
        counts[gift["fund"], gift["region"]] += 1
    rows = run(sources, query)
    assert {(r["gifts.fund"], r["gifts.region"]) for r in rows} == {
        k for k, n in counts.items() if n >= 34
    }
    assert 0 < len(rows) < len(counts)
    assert " HAVING COUNT(*) >= 34" in compiled(sources, query).sql


def test_filter_problems_are_reported_with_paths(sources: dict[str, Source]) -> None:
    def check(*filters: dict[str, Any]) -> dict[str, str]:
        return problems(
            sources,
            {"dataset": "gifts", "measures": ["gifts.gift_count"], "filters": list(filters)},
        )

    assert "/filters/0/values/0" in check(
        {"field": "gifts.amount_value", "op": "gt", "values": ["lots"]}
    )
    assert "/filters/0/values/0" in check(
        {"field": "gifts.gift_date", "op": "gte", "values": ["01/02/2026"]}
    )
    assert "/filters/0/op" in check(
        {"field": "gifts.amount_value", "op": "contains", "values": ["1"]}
    )
    assert "/filters/0/op" in check(
        {"field": "gifts.total_amount", "op": "contains", "values": ["1"]}
    )
    assert "/filters/0/op" in check(
        {"field": "gifts.fund", "op": "in_range", "range": {"relative": "today"}}
    )
    assert "/filters/1/field" in check(
        {"field": "gifts.fund", "op": "eq", "values": ["x"]},
        {"field": "gifts.nope", "op": "eq", "values": ["x"]},
    )


def test_segments(sources: dict[str, Source]) -> None:
    query = {
        "dataset": "gifts",
        "measures": ["gifts.gift_count"],
        "segments": ["gifts.large", "gifts.online"],
    }
    assert run(sources, query)[0]["gifts.gift_count"] == sum(
        g["amount"] >= 500 and g["channel"] == "online" for g in GIFTS
    )
    assert "/segments/0" in problems(sources, {**query, "segments": ["gifts.nope"]})


def test_sort_limit_offset_and_truncation(sources: dict[str, Source]) -> None:
    query = {
        "dataset": "gifts",
        "dimensions": ["gifts.amount_value"],
        "sort": [{"field": "gifts.amount_value", "dir": "desc"}],
        "limit": 5,
    }
    amounts = sorted({g["amount"] for g in GIFTS}, reverse=True)
    result = ENGINE.execute(compiled(sources, query))
    assert [r["gifts.amount_value"] for r in result.table.to_pylist()] == amounts[:5]
    assert result.truncated and result.row_count == 5
    page = run(sources, {**query, "offset": 5})
    assert [r["gifts.amount_value"] for r in page] == amounts[5:10]

    everything = ENGINE.execute(compiled(sources, {**query, "limit": 10_000}))
    assert everything.row_count == len(amounts) and not everything.truncated
    # The server cap wins over the requested limit, and the default limit is 500.
    assert ENGINE.execute(compiled(sources, {**query, "limit": 10_000}, max_rows=7)).row_count == 7
    assert compiled(sources, {"dataset": "gifts", "dimensions": ["gifts.fund"]}).row_limit == 500
    assert "/sort/0/field" in problems(sources, {**query, "sort": [{"field": "gifts.fund"}]})


# ---- joins --------------------------------------------------------------------------


def test_join_to_a_dimension_of_another_dataset(sources: dict[str, Source]) -> None:
    rows = run(
        sources,
        {
            "dataset": "gifts",
            "dimensions": ["donors.tier"],
            "measures": ["gifts.total_amount", "gifts.gift_count"],
        },
    )
    tier = {d["id"]: d["tier"] for d in DONORS}
    expected: dict[str | None, Decimal] = defaultdict(Decimal)
    for gift in GIFTS:
        expected[tier.get(gift["donor_id"])] += gift["amount"]
    assert {r["donors.tier"]: r["gifts.total_amount"] for r in rows} == dict(expected)
    assert None in expected  # gifts whose donor is missing are kept by the left join
    assert sum(r["gifts.gift_count"] for r in rows) == 400  # the join never duplicates gifts


def test_fields_two_joins_away(sources: dict[str, Source], tmp_path: Path) -> None:
    """gifts -> donors -> regions: each hop many_to_one, so no gift is counted twice."""
    regions = [{"code": "VIC", "state": "Victoria"}, {"code": "NSW", "state": "New South Wales"}]
    to_regions = {"to": "regions", "on": {"region": "code"}, "relationship": "many_to_one"}

    def donors_with(*joins: dict[str, Any], restricted: bool = False) -> Source:
        definition = {**DONORS_MODEL, "joins": [*DONORS_MODEL["joins"], *joins]}
        return Source(semantic.parse_model("donors", definition), DONOR_COLUMNS,
                      sources["donors"].table, restricted=restricted)  # fmt: skip

    three = {
        **sources,
        "donors": donors_with(to_regions),
        "regions": Source(
            semantic.parse_model(
                "regions", {"dimensions": {"state": {"column": "state", "type": "string"}}}
            ),
            {"code": "string", "state": "string"},
            TableRef("regions", "d3", "s3", write_parquet(tmp_path / "regions", regions)),
        ),
    }
    query = {
        "dataset": "gifts",
        "dimensions": ["regions.state"],
        "measures": ["gifts.total_amount", "gifts.gift_count"],
    }
    rows = run(three, query)
    donor_region = {d["id"]: d["region"] for d in DONORS}
    state = {r["code"]: r["state"] for r in regions}
    expected: dict[str | None, Decimal] = defaultdict(Decimal)
    for gift in GIFTS:
        expected[state.get(str(donor_region.get(gift["donor_id"])))] += gift["amount"]
    assert {r["regions.state"]: r["gifts.total_amount"] for r in rows} == dict(expected)
    assert sum(r["gifts.gift_count"] for r in rows) == 400  # left joins keep every gift
    assert None in expected  # QLD donors, and gifts whose donor is missing
    sql = compiled(three, query).sql
    assert sql.count("LEFT JOIN") == 2 and '"donors"."region" = "regions"."code"' in sql

    # A filter on a field two joins away.
    filtered = run(three, {"dataset": "gifts", "measures": ["gifts.gift_count"],
                           "filters": [{"field": "regions.state", "op": "in", "values": ["Victoria"]}]})  # fmt: skip
    assert filtered[0]["gifts.gift_count"] == sum(
        1 for g in GIFTS if donor_region.get(g["donor_id"]) == "VIC"
    )

    # Two ways there, as short as each other: refused, with both named.
    agents = Source(
        semantic.parse_model("agents", {"dimensions": {}, "joins": [to_regions]}),
        {"id": "integer", "region": "string"},
        TableRef("agents", "d4", "s4", str(tmp_path / "agents")),
    )
    gifts_two = Source(
        semantic.parse_model("gifts", {**GIFTS_MODEL, "joins": [*GIFTS_MODEL["joins"],
                             {"to": "agents", "on": {"donor_id": "id"}, "relationship": "many_to_one"}]}),
        GIFT_COLUMNS, sources["gifts"].table,
    )  # fmt: skip
    ways = problems({**three, "gifts": gifts_two, "agents": agents}, query)["/dimensions/0"]
    assert "two paths as short as each other" in ways
    assert "gifts → donors → regions" in ways and "gifts → agents → regions" in ways

    # Never through a restricted dataset, nor along a relationship the other way (a v1
    # one_to_many join becomes an inactive relationship from regions to donors).
    hidden = problems({**three, "donors": donors_with(to_regions, restricted=True)}, query)
    assert "restricted" in hidden["/dimensions/0"]
    fan_out = {**to_regions, "relationship": "one_to_many"}
    turned = problems({**three, "donors": donors_with(fan_out)}, query)["/dimensions/0"]
    assert "does not reach through active many-to-one relationships" in turned
    # Three steps away (v2 has no limit on the number of steps).
    far = {**three, "regions": Source(
        semantic.parse_model("regions", {"dimensions": {}, "joins": [
            {"to": "countries", "on": {"code": "region"}, "relationship": "many_to_one"}]}),
        {"code": "string"}, three["regions"].table,
    ), "countries": Source(
        semantic.parse_model("countries", {"dimensions": {"name": {"column": "name", "type": "string"}}}),
        {"name": "string", "region": "string"}, TableRef("countries", "d5", "s5", str(tmp_path / "c")),
    )}  # fmt: skip
    three_steps = compiled(far, {"dataset": "gifts", "dimensions": ["countries.name"]}).sql
    assert three_steps.count("LEFT JOIN") == 3
    assert '"regions"."code" = "countries"."region"' in three_steps


def test_unsafe_or_missing_joins_are_refused(sources: dict[str, Source]) -> None:
    fan_out = problems(
        sources,
        {"dataset": "donors", "dimensions": ["gifts.fund"], "measures": ["donors.donor_count"]},
    )
    assert "on the many side of 'donors'" in fan_out["/dimensions/0"]
    assert "more than once" in fan_out["/dimensions/0"]
    no_join = problems(sources, {"dataset": "donors", "dimensions": ["events.name"]})
    assert "not a table of the model" in no_join["/dimensions/0"]
    # A measure of another table is computed from its own table (v2).
    other_measure = run(sources, {"dataset": "gifts", "measures": ["donors.donor_count"]})
    assert other_measure == [{"donors.donor_count": len(DONORS)}]
    assert "/dimensions/0" in problems(
        sources, {"dataset": "gifts", "dimensions": ["gifts.total_amount"]}
    )
    assert "/measures/0" in problems(sources, {"dataset": "gifts", "measures": ["gifts.fund"]})
    with pytest.raises(ApiError) as info:
        compiled(sources, {"dataset": "nope", "measures": ["nope.x"]})
    assert info.value.details[0]["path"] == "/dataset"


# ---- row-level security ---------------------------------------------------------------

BY_REGION = {"dataset": "gifts", "dimensions": ["gifts.region"], "measures": ["gifts.gift_count"]}


def regions_seen(
    sources: dict[str, Source], user: UserContext, query: dict[str, Any] = BY_REGION
) -> set[str]:
    return {r["gifts.region"] for r in run(sources, query, user)}


def test_rls_filters_rows_by_user_attribute(sources: dict[str, Source]) -> None:
    assert regions_seen(sources, ADMIN) == {"VIC", "NSW", "QLD"}  # admins are exempt by default
    assert regions_seen(sources, UserContext("viewer", attributes={"regions": ["VIC"]})) == {"VIC"}
    assert regions_seen(sources, UserContext("editor", attributes={"regions": ["VIC", "QLD"]})) == {
        "VIC",
        "QLD",
    }
    assert regions_seen(sources, UserContext("viewer", attributes={"regions": "NSW"})) == {"NSW"}

    vic = UserContext("viewer", attributes={"regions": ["VIC"]})
    total = run(sources, {"dataset": "gifts", "measures": ["gifts.total_amount"]}, vic)
    assert total[0]["gifts.total_amount"] == sum(g["amount"] for g in GIFTS if g["region"] == "VIC")


@pytest.mark.parametrize(
    "attributes",
    [None, {}, {"regions": []}, {"regions": None}, {"regions": ""}, {"regions": {"a": 1}},
     {"regions": [["VIC"]]}, {"other": ["VIC"]}],
)  # fmt: skip
def test_rls_denies_when_the_attribute_is_missing_or_unusable(
    sources: dict[str, Source], attributes: dict[str, Any] | None
) -> None:
    viewer = UserContext("viewer", attributes=attributes)
    assert run(sources, BY_REGION, viewer) == []
    assert run(sources, {"dataset": "gifts", "measures": ["gifts.gift_count"]}, viewer) == [
        {"gifts.gift_count": 0}
    ]


def test_rls_cannot_be_widened_by_the_query_or_by_attribute_values(
    sources: dict[str, Source],
) -> None:
    vic = UserContext("viewer", attributes={"regions": ["VIC"]})
    # Asking for another region returns nothing rather than that region.
    other = {**BY_REGION, "filters": [{"field": "gifts.region", "op": "eq", "values": ["NSW"]}]}
    assert run(sources, other, vic) == []
    widen = {
        **BY_REGION,
        "filters": [{"field": "gifts.region", "op": "in", "values": ["VIC", "NSW", "QLD"]}],
    }
    assert regions_seen(sources, vic, widen) == {"VIC"}
    # SQL in a filter value or in an attribute is only ever a string to compare with.
    sneaky = {
        **BY_REGION,
        "filters": [{"field": "gifts.region", "op": "eq", "values": ["x' OR '1'='1"]}],
    }
    assert run(sources, sneaky, vic) == []
    for value in ["VIC' OR '1'='1", "VIC') OR (1=1", "'; DROP TABLE gifts; --", "VIC\\' OR 1=1 --"]:
        assert run(sources, BY_REGION, UserContext("viewer", attributes={"regions": [value]})) == []
        assert run(sources, BY_REGION, UserContext("viewer", attributes={"regions": value})) == []
    # The rule lives inside the subquery that reads the table.
    sql = compiled(sources, other, vic).sql
    assert """read_parquet""" in sql.lower()
    assert """AS "gifts" WHERE ("gifts"."region" IN ('VIC'))) AS "gifts" WHERE""" in sql


def test_rls_rules_roles_user_fields_and_joined_datasets(tmp_path: Path) -> None:
    donors_path = write_parquet(tmp_path / "donors", DONORS)
    gifts_path = write_parquet(tmp_path / "gifts", GIFTS, ("amount",))

    def build(
        gifts_rls: list[dict[str, Any]], donors_rls: list[dict[str, Any]]
    ) -> dict[str, Source]:
        gifts = {**GIFTS_MODEL, "rls": gifts_rls}
        donors = {**DONORS_MODEL, "rls": donors_rls}
        assert (
            semantic.validate(SPEC, "gifts", gifts, GIFT_COLUMNS, {"donors": DONOR_COLUMNS}) == []
        )
        assert (
            semantic.validate(SPEC, "donors", donors, DONOR_COLUMNS, {"gifts": GIFT_COLUMNS}) == []
        )
        return {
            "gifts": Source(
                semantic.parse_model("gifts", gifts),
                GIFT_COLUMNS,
                TableRef("gifts", "a", "b", gifts_path),
            ),
            "donors": Source(
                semantic.parse_model("donors", donors),
                DONOR_COLUMNS,
                TableRef("donors", "c", "d", donors_path),
            ),
        }

    # Several rules are ANDed; a rule can target admins; scalars and user fields work.
    strict = build(
        [
            {
                "filter": "region IN {{ user.attributes.regions }}",
                "applies_to_roles": ["viewer", "editor", "admin"],
            },
            {"filter": "amount < {{ user.attributes.max_amount }}", "applies_to_roles": ["viewer"]},
            {
                "filter": "{{ user.role }} = 'viewer' OR fund <> 'Sport'",
                "applies_to_roles": ["editor"],
            },
        ],
        [],
    )
    count = {"dataset": "gifts", "measures": ["gifts.gift_count"]}
    assert run(strict, count, ADMIN) == [{"gifts.gift_count": 0}]  # admin has no regions here
    viewer = UserContext("viewer", attributes={"regions": ["VIC"], "max_amount": 300})
    assert run(strict, count, viewer)[0]["gifts.gift_count"] == sum(
        g["region"] == "VIC" and g["amount"] < 300 for g in GIFTS
    )
    editor = UserContext("editor", attributes={"regions": ["VIC", "NSW", "QLD"]})
    assert run(strict, count, editor)[0]["gifts.gift_count"] == sum(
        g["fund"] != "Sport" for g in GIFTS
    )

    # A rule on a related table hides its rows and, through the relationship, the rows
    # that relate to them (R2, ADR-032): only O'Brien's gifts are left, and a gift whose
    # donor is missing matches no visible donor.
    joined = build([], [{"filter": "name = {{ user.email }}"}])
    rows = run(
        joined,
        {"dataset": "gifts", "dimensions": ["donors.name"], "measures": ["gifts.gift_count"]},
        UserContext("viewer", email="O'Brien"),
    )
    assert {r["donors.name"] for r in rows} == {"O'Brien"}
    assert sum(r["gifts.gift_count"] for r in rows) == sum(g["donor_id"] == 3 for g in GIFTS)


def test_a_broken_rls_rule_blocks_the_dataset_instead_of_failing_open(tmp_path: Path) -> None:
    broken = semantic.parse_model(
        "gifts", {**GIFTS_MODEL, "rls": [{"filter": "no_such_column = 1"}]}
    )
    source = {"gifts": Source(broken, GIFT_COLUMNS, TableRef("gifts", "a", "b", str(tmp_path)))}
    with pytest.raises(ApiError) as info:
        compiled(source, BY_REGION, UserContext("viewer"))
    assert info.value.status == 409 and info.value.code == "invalid_model"
    compiled(source, BY_REGION, ADMIN)  # the rule does not apply to admins


# ---- expression allow-list ------------------------------------------------------------

REJECTED = [
    ("(SELECT max(amount) FROM gifts)", "Subqueries"),
    ("amount IN (SELECT 1)", "IN must be followed by a list"),
    ("read_csv('/etc/passwd')", "READ_CSV is not allowed"),
    ("read_parquet('/data/secrets/x')", "not allowed"),
    ("getenv('ENCRYPTION_KEY')", "GETENV is not allowed"),
    ("current_setting('memory_limit')", "not allowed"),
    ("amount; DROP TABLE gifts", "single expression"),
    ("amount); DROP TABLE gifts; --", "not a valid SQL expression"),
    ("sum(amount)", "Aggregate functions"),
    ("sum(amount) OVER ()", "Window functions"),
    ("row_number() OVER ()", "Window functions"),
    ("gifts.amount", "without a table prefix"),
    ("other_schema.gifts.amount", "without a table prefix"),
    ("no_such_column + 1", "no column called 'no_such_column'"),
    ("amount = $1", "parameters"),
    ("amount = ?", "parameters"),
    ("CAST(amount AS BLOB)", "Casting to"),
    ("amount::INTEGER[]", "not allowed"),
    ("{'a': amount}", "not allowed"),
    ("current_date", "not allowed"),
    ("now()", "NOW is not allowed"),
    ("random()", "not allowed"),
    ("amount" + " + 1" * 500, "longer than"),
]


@pytest.mark.parametrize(("text", "reason"), REJECTED)
def test_unsafe_expressions_are_rejected(text: str, reason: str) -> None:
    with pytest.raises(ExpressionError) as info:
        expressions.column_expression(text, "gifts", list(GIFT_COLUMNS))
    assert reason in str(info.value), str(info.value)


def test_allowed_expressions_compile_and_bind_columns() -> None:
    cases = {
        "upper(FUND) || '-' || region": 'UPPER("gifts"."fund") || \'-\' || "gifts"."region"',
        "amount * 1.1 -- sneaky comment": '"gifts"."amount" * 1.1',
        "coalesce(channel, 'none')": 'COALESCE("gifts"."channel", \'none\')',
        "amount between 1 and 5 or region in ('VIC', 'NSW')": None,
        "CASE WHEN amount > 100 THEN 'big' WHEN amount IS NULL THEN NULL ELSE 'small' END": None,
        "CAST(amount AS INTEGER) % 2 = 0 AND NOT region LIKE 'V%'": None,
        "date_trunc('month', gift_date)": None,
        "year(gift_date) * 100 + month(gift_date)": None,
        "datediff('day', gift_date, created_at)": None,
        "round(abs(amount) / nullif(id, 0), 2)": None,
        "length(trim(lower(fund)))": None,
        "strftime(gift_date, '%Y-%m')": None,
    }
    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE gifts (id INT, donor_id INT, fund TEXT, amount DECIMAL(12,2), gift_date DATE, "
        "region TEXT, channel TEXT, created_at TIMESTAMP)"
    )
    for text, expected in cases.items():
        sql = expressions.column_expression(text, "gifts", list(GIFT_COLUMNS)).sql(
            dialect="duckdb", comments=False
        )
        if expected is not None:
            assert sql == expected
        assert "--" not in sql and "/*" not in sql
        conn.execute(f"SELECT {sql} FROM gifts").fetchall()  # the output is valid DuckDB


def test_measure_formulas_may_only_combine_measures() -> None:
    total = exp.Sum(this=exp.column("amount", table="gifts", quoted=True))
    tree = expressions.measure_expression("{a} / NULLIF({b}, 0) * 100", lambda name: total.copy())
    assert tree.sql(dialect="duckdb").count('SUM("gifts"."amount")') == 2
    for text, reason in [
        ("amount / 2", "only use other measures"),
        ("{a} + (SELECT 1)", "Subqueries"),
        ("{a} + sum(amount)", "Aggregate functions"),
        ("{a} + {Bad Name}", "Reference measures as {measure_name}"),
        ("{a}; DROP TABLE x", "single expression"),
    ]:
        with pytest.raises(ExpressionError) as info:
            expressions.measure_expression(text, lambda name: total.copy())
        assert reason in str(info.value)


def test_rls_expressions_only_accept_known_placeholders() -> None:
    columns = list(GIFT_COLUMNS)
    for text in [
        "region = {{ user.password }}",
        "region = {{ settings.key }}",
        "region = {{ 1 + 1 }}",
    ]:
        with pytest.raises(ExpressionError, match="user.attributes"):
            expressions.rls_expression(text, "gifts", columns, None)
    with pytest.raises(ExpressionError, match="Subqueries|IN must be followed by a list"):
        expressions.rls_expression("region IN (SELECT {{ user.email }})", "gifts", columns, None)
    assert expressions.user_references("a = {{user.email}} AND b IN {{ user.attributes.x_1 }}") == [
        "email",
        "attributes.x_1",
    ]
    done = expressions.rls_expression(
        "region IN {{ user.attributes.r }} AND fund = {{ user.attributes.f }} AND id > {{ user.attributes.n }}",
        "gifts", columns, {"attributes.r": ["A", "it's"], "attributes.f": "Sport", "attributes.n": 5},
    )  # fmt: skip
    assert done.sql(dialect="duckdb") == (
        '"gifts"."region" IN (\'A\', \'it\'\'s\') AND "gifts"."fund" = \'Sport\' AND "gifts"."id" > 5'
    )


# ---- model validation and starter models ----------------------------------------------


def model_problems(definition: Any, dataset: str = "gifts") -> dict[str, str]:
    found = semantic.validate(SPEC, dataset, definition, GIFT_COLUMNS, {"donors": DONOR_COLUMNS})
    return {p["path"]: p["message"] for p in found}


def test_model_validation_reports_every_problem_with_a_path() -> None:
    found = model_problems(
        {
            "dimensions": {
                "fund": {"column": "Fund", "type": "string"},
                "when": {"column": "fund", "type": "time"},
                "bad_expr": {"expr": "(SELECT 1)", "type": "number"},
                "total": {"column": "amount", "type": "number", "grains": ["month"]},
            },
            "measures": {
                "total": {"agg": "sum", "column": "amount"},
                "text_sum": {"agg": "sum", "column": "fund"},
                "no_column": {"agg": "max"},
                "missing_ref": {"expr": "{nope} * 2"},
                "raw": {"expr": "amount * 2"},
                "loop_a": {"expr": "{loop_b} + 1"},
                "loop_b": {"expr": "{loop_a} + 1"},
            },
            "segments": {"bad": {"filter": "read_csv('x')"}},
            "joins": [
                {"to": "donors", "on": {"donor_id": "id", "nope": "id", "fund": "nope"}},
                {"to": "donors", "on": {"donor_id": "id"}},
                {"to": "missing", "on": {"donor_id": "id"}},
                {"to": "gifts", "on": {"id": "id"}},
            ],
            "rls": [{"filter": "region = {{ user.attributes.region }}"}, {"filter": "nope = 1"}],
        }
    )
    assert (
        found["/dimensions/fund/column"] == "There is no column called 'Fund'. Did you mean 'fund'?"
    )
    assert "not a date or timestamp" in found["/dimensions/when/type"]
    assert "Subqueries" in found["/dimensions/bad_expr/expr"]
    assert "cannot share a name" in found["/dimensions/total"]
    assert "Only time dimensions" in found["/dimensions/total/grains"]
    assert "numeric column" in found["/measures/text_sum/column"]
    assert found["/measures/no_column"] == "max needs a `column`."
    assert "no measure called 'nope'" in found["/measures/missing_ref/expr"]
    assert "only use other measures" in found["/measures/raw/expr"]
    assert "refers to itself" in found["/measures/loop_a/expr"]
    assert "refers to itself" in found["/measures/loop_b/expr"]
    assert "READ_CSV" in found["/segments/bad/filter"]
    assert "no column called 'nope'" in found["/joins/0/on/nope"]
    assert "has no column called 'nope'" in found["/joins/0/on/fund"]
    assert "already a join" in found["/joins/1/to"]
    assert "no dataset called 'missing'" in found["/joins/2/to"]
    assert "cannot join to itself" in found["/joins/3/to"]
    assert "/rls/0/filter" not in found and "no column called 'nope'" in found["/rls/1/filter"]


def test_model_structure_is_checked_first() -> None:
    assert model_problems([]) == {"/": "The model must be a mapping."}
    found = model_problems(
        {
            "dimensions": {"Bad Name": {"column": "fund", "type": "string"}, "x": {"type": "string"},
                           "y": {"column": "fund", "expr": "fund", "type": "string"}, "z": {"column": "fund", "type": "text"}},
            "measures": {"m": {"agg": "sum", "column": "amount", "expr": "{m}"}, "n": {"agg": "median", "column": "amount"}},
            "rls": [{"filter": "region = 'VIC'", "applies_to_roles": ["everyone"]}],
            "extra": 1,
        }
    )  # fmt: skip
    assert "exactly one of `column` or `expr`" in found["/dimensions/x"]
    assert "exactly one of `column` or `expr`" in found["/dimensions/y"]
    assert "/dimensions/z/type" in found and "/dimensions" in found  # bad enum; bad property name
    assert "either `agg`" in found["/measures/m"] and "/measures/n/agg" in found
    assert "/rls/0/applies_to_roles/0" in found and "/" in found
    assert model_problems({"dataset": "donors"}) == {
        "/dataset": "This model belongs to 'gifts', not 'donors'."
    }
    assert model_problems({"dataset": "gifts"}) == {}


def test_starter_model_is_valid_and_queryable(tmp_path: Path) -> None:
    columns = {"id": "integer", "Donor ID": "integer", "Gift Date": "date", "amount": "decimal",
               "2024 total": "float", "Fund": "string", "FUND 2": "string", "paid": "boolean", "meta": "json"}  # fmt: skip
    starter = semantic.starter_definition("My gifts", columns, primary_key=("id",))
    assert semantic.validate(SPEC, "g", starter, columns, {}) == []
    assert starter["dimensions"]["gift_date"] == {
        "column": "Gift Date",
        "type": "time",
        "label": "Gift Date",
    }
    assert starter["dimensions"]["f_2024_total"]["column"] == "2024 total"
    assert {starter["dimensions"][n]["column"] for n in ("fund", "fund_2")} == {"Fund", "FUND 2"}
    assert (
        starter["dimensions"]["paid"]["type"] == "boolean"
        and starter["dimensions"]["meta"]["type"] == "string"
    )
    assert starter["measures"] == {
        "row_count": {"agg": "count", "label": "Rows"},
        "total_amount": {"agg": "sum", "column": "amount", "label": "Total amount"},
        "total_2024_total": {"agg": "sum", "column": "2024 total", "label": "Total 2024 total"},
    }  # no sums for id columns

    rows = [{"id": 1, "Donor ID": 7, "Gift Date": date(2026, 8, 3), "amount": Decimal("5.50"),
             "2024 total": 2.5, "Fund": "A", "FUND 2": "a", "paid": True, "meta": "{}"}]  # fmt: skip
    source = {
        "g": Source(
            semantic.parse_model("g", starter),
            columns,
            TableRef("g", "a", "b", write_parquet(tmp_path / "g", rows, ("amount",))),
        )
    }
    result = run(
        source,
        {"dataset": "g", "dimensions": ["g.fund", "g.fund_2"], "measures": ["g.row_count", "g.total_2024_total"],
         "timeDimension": {"field": "g.gift_date", "grain": "fiscal_year"}},
    )  # fmt: skip
    assert result == [
        {
            "g.fund": "A",
            "g.fund_2": "a",
            "g.gift_date": date(2026, 7, 1),
            "g.row_count": 1,
            "g.total_2024_total": 2.5,
        }
    ]


# ---- engine and cache -----------------------------------------------------------------


def test_columns_that_differ_only_by_case_are_stored_under_distinct_names(tmp_path: Path) -> None:
    from opsis.connectors.base import RefreshMode
    from opsis.storage import snapshots
    from tests.conftest import make_settings

    assert snapshots.distinct_names(["Fund", "fund", "FUND", "fund_2", "x"]) == [
        "Fund", "fund_2", "FUND_3", "fund_2_2", "x",
    ]  # fmt: skip
    batch = pa.RecordBatch.from_arrays([pa.array(["A"]), pa.array(["a"])], names=["Fund", "fund"])
    built = snapshots.build_snapshot(
        make_settings(data_dir=tmp_path), __import__("uuid").uuid4(), iter([batch]),
        mode=RefreshMode.FULL, previous=None,
    )  # fmt: skip
    assert [c["name"] for c in built.columns] == ["Fund", "fund_2"]
    assert snapshots.preview(built.path, 5) == [["A", "a"]]


def test_engine_stops_a_query_at_the_timeout() -> None:
    slow = CompiledQuery(
        "SELECT count(*) FROM range(100000000) a, range(100000000) b", (), (), 10, 0.3
    )
    started = time.monotonic()
    with pytest.raises(QueryTimeoutError, match="0.3 seconds"):
        ENGINE.execute(slow)
    assert time.monotonic() - started < 5


def test_engine_describes_snapshots(sources: dict[str, Source]) -> None:
    table = sources["gifts"].table
    assert ENGINE.row_count(table) == 400
    assert {c.name: c.type.value for c in ENGINE.describe(table)} == GIFT_COLUMNS


def test_result_cache_is_keyed_by_sql_and_evicts_the_oldest(sources: dict[str, Source]) -> None:
    cache = ResultCache(max_entries=2)
    calls: list[str] = []

    def fetch(query: CompiledQuery) -> QueryResult:
        def run_it() -> QueryResult:
            calls.append(query.sql)
            return ENGINE.execute(query)

        return cache.get_or_run(query.cache_key(), run_it)

    vic = compiled(sources, BY_REGION, UserContext("viewer", attributes={"regions": ["VIC"]}))
    nsw = compiled(sources, BY_REGION, UserContext("viewer", attributes={"regions": ["NSW"]}))
    admin = compiled(sources, BY_REGION, ADMIN)
    assert len({vic.cache_key(), nsw.cache_key(), admin.cache_key()}) == 3  # RLS is part of the key

    first, again = fetch(vic), fetch(vic)
    assert not first.cache_hit and again.cache_hit and len(calls) == 1
    assert (
        again.table.to_pylist()
        == first.table.to_pylist()
        == [{"gifts.region": "VIC", "gifts.gift_count": 133}]
    )
    fetch(nsw)
    fetch(admin)  # evicts vic, the least recently used
    assert not fetch(vic).cache_hit and len(calls) == 4
    assert (cache.hits, cache.misses) == (1, 4)

    # A new snapshot has a new path, so the key changes and the old entry is never used.
    moved = Source(
        sources["gifts"].model, GIFT_COLUMNS, TableRef("gifts", "d1", "s2", "/data/other")
    )
    assert compiled({**sources, "gifts": moved}, BY_REGION).cache_key() != admin.cache_key()
    # A relative range resolves to dates, so tomorrow's query is a different key.
    ranged = {
        **BY_REGION,
        "timeDimension": {"field": "gifts.gift_date", "range": {"relative": "last_30_days"}},
    }
    assert (
        compiled(sources, ranged).cache_key()
        != compiled(sources, ranged, today=TODAY + timedelta(days=1)).cache_key()
    )
