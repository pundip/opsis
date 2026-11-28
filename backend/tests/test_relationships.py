"""Relationships in queries (V2, ADR-031), each checked against hand-written SQL over
the same rows: fields of related tables in one query, left and inner joins, keys of
more than one column, a measure that names an inactive relationship, filters that
travel both ways, and two steps."""

from __future__ import annotations

from typing import Any

from tests.test_ingestion import make_dataset, run_jobs
from tests.test_query_api import query
from tests.test_row_security import DONORS_REGIONS, GIFTS_DONORS, World, end
from tests.test_row_security import r2 as r2  # noqa: F401  (fixture)
from tests.test_row_security import world as world  # noqa: F401  (fixture)

WEB = [{"field": "gifts.channel", "op": "eq", "values": ["web"]}]


def grouped(world: World, body: dict[str, Any]) -> dict[Any, tuple[Any, ...]]:
    """The first column of each row, to the rest (numbers as floats)."""
    found = query(world.admin, body)["rows"]
    return {row[0]: tuple(None if v is None else float(v) for v in row[1:]) for row in found}


def expected(world: World, sql: str) -> dict[Any, tuple[Any, ...]]:
    return {
        row[0]: tuple(None if v is None else float(v) for v in row[1:])
        for row in world.conn.execute(sql)
    }


def test_fields_of_two_tables_in_one_query_with_left_and_inner_joins(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    by_region = {
        "dataset": "gifts",
        "dimensions": ["donors.region"],
        "measures": ["gifts.row_count", "gifts.total_amount"],
    }
    left = expected(
        world,
        "SELECT d.region, count(*), sum(g.amount) FROM r2.gifts g "
        "LEFT JOIN r2.donors d ON d.id = g.donor_id GROUP BY 1",
    )
    assert grouped(world, by_region) == left
    assert left[None][0] == 18  # no donor, or one that does not exist: kept, with no region

    inner = {**GIFTS_DONORS, "joinType": "inner"}
    world.change({"op": "replace", "path": "/relationships/gifts__donors", "value": inner})
    assert grouped(world, by_region) == {k: v for k, v in left.items() if k is not None}


def test_keys_of_more_than_one_column(world: World) -> None:
    world.conn.execute(
        "CREATE TABLE r2.channel_goals (donor_id integer, channel text, goal text, "
        "PRIMARY KEY (donor_id, channel))"
    )
    world.conn.execute(
        "INSERT INTO r2.channel_goals SELECT n, c, CASE WHEN n % 2 = 0 THEN 'high' ELSE 'low' END "
        "FROM generate_series(1, 12) n, unnest(ARRAY['web', 'mail']) c"
    )
    make_dataset(world.admin, world.connection, "r2.channel_goals", name="channel_goals")
    run_jobs(world.admin)
    goals = {
        "from": end("gifts", "donor_id", "channel"),
        "to": end("channel_goals", "donor_id", "channel"),
    }
    world.relate("gifts__channel_goals", goals)
    by_goal = {
        "dataset": "gifts",
        "dimensions": ["channel_goals.goal"],
        "measures": ["gifts.row_count"],
    }
    assert grouped(world, by_goal) == expected(
        world,
        "SELECT c.goal, count(*) FROM r2.gifts g LEFT JOIN r2.channel_goals c "
        "ON c.donor_id = g.donor_id AND c.channel = g.channel GROUP BY 1",
    )


def test_a_measure_can_name_an_inactive_relationship(world: World) -> None:
    referrers = {"from": end("gifts", "referrer_id"), "to": end("donors", "id"), "active": False}
    world.change(
        {"op": "add", "path": "/relationships/gifts__donors", "value": GIFTS_DONORS},
        {"op": "add", "path": "/relationships/gifts__donors_2", "value": referrers},
        {"op": "add", "path": "/tables/gifts/measures/referred",
         "value": {"agg": "count", "relationship": "gifts__donors_2"}},
    )  # fmt: skip
    found = grouped(
        world,
        {"dataset": "gifts", "dimensions": ["donors.region"],
         "measures": ["gifts.row_count", "gifts.referred"]},
    )  # fmt: skip
    by_donor = expected(
        world,
        "SELECT d.region, count(*) FROM r2.gifts g LEFT JOIN r2.donors d ON d.id = g.donor_id "
        "GROUP BY 1",
    )
    by_referrer = expected(
        world,
        "SELECT r.region, count(*) FROM r2.gifts g LEFT JOIN r2.donors r ON r.id = g.referrer_id "
        "GROUP BY 1",
    )
    for region in ("VIC", "NSW", "QLD"):
        assert found[region] == (by_donor[region][0], by_referrer[region][0])
    # Gifts with no known donor have no region by their donor; every referrer is known.
    assert found[None][0] == by_donor[None][0] and found[None][1] in (None, 0)
    assert None not in by_referrer


def test_filters_reach_the_one_side_only_when_they_travel_both_ways(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    vic = [{"field": "donors.region", "op": "eq", "values": ["VIC"]}]
    gifts = {"dataset": "gifts", "measures": ["gifts.row_count"], "filters": vic}
    assert query(world.admin, gifts)["rows"] == [
        [world.count("SELECT count(*) FROM r2.gifts g JOIN r2.donors d ON d.id = g.donor_id "
                     "WHERE d.region = 'VIC'")]
    ]  # fmt: skip

    donors = {"dataset": "donors", "measures": ["donors.row_count"], "filters": WEB}
    refused = query(world.admin, donors, status=422)
    [problem] = refused["error"]["details"]
    assert "'gifts.channel' does not filter 'donors'" in problem["message"]

    both = {**GIFTS_DONORS, "crossFilter": "both"}
    world.change({"op": "replace", "path": "/relationships/gifts__donors", "value": both})
    assert query(world.admin, donors)["rows"] == [
        [world.count("SELECT count(*) FROM r2.donors d WHERE EXISTS (SELECT 1 FROM r2.gifts g "
                     "WHERE g.donor_id = d.id AND g.channel = 'web')")]
    ]  # fmt: skip


def test_fields_two_steps_away(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.relate("donors__regions", DONORS_REGIONS)
    by_name = {
        "dataset": "gifts",
        "dimensions": ["regions.name"],
        "measures": ["gifts.total_amount"],
    }
    assert grouped(world, by_name) == expected(
        world,
        "SELECT r.name, sum(g.amount) FROM r2.gifts g LEFT JOIN r2.donors d ON d.id = g.donor_id "
        "LEFT JOIN r2.regions r ON r.code = d.region GROUP BY 1",
    )
