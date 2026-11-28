"""Row-level security through relationships (R2, ADR-032 and V2_DESIGN 3.5): a rule on
a table hides the rows of every table it filters through active relationships, for
every way rows are read: queries, dashboards, share links, previews, profiles, filter
options and the agent's samples. Admins are exempt and say so; share links never are.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from opsis.settings import Settings
from tests.test_auth import SERVER_SIGN_IN, admin_client, person
from tests.test_ingestion import connect, make_dataset, ok, run_jobs
from tests.test_mcp import Mcp
from tests.test_query_api import query

ClientFor = Callable[..., TestClient]

REGIONS = {"regions": ["VIC"]}
EVERYWHERE = {"regions": ["VIC", "NSW", "QLD"]}
DONORS_RULE = [{"filter": "region IN {{ user.attributes.regions }}"}]
REGIONS_RULE = [{"filter": "code IN {{ user.attributes.regions }}"}]
COUNT = {"dataset": "gifts", "measures": ["gifts.row_count"]}
CODES = {"dataset": "gifts", "dimensions": ["gifts.gift_code"], "measures": ["gifts.row_count"]}


def end(table: str, *columns: str) -> dict[str, Any]:
    return {"table": table, "columns": list(columns)}


GIFTS_DONORS = {"from": end("gifts", "donor_id"), "to": end("donors", "id")}
DONORS_REGIONS = {"from": end("donors", "region"), "to": end("regions", "code")}


@pytest.fixture
def r2(migrated_db: Settings) -> Iterator[dict[str, Any]]:
    """regions, donors, gifts and days in the test Postgres.

    Donors 1 to 12 are in VIC (3, 6, 9, 12), NSW (1, 4, 7, 10) and QLD. Of the 60 gifts,
    every tenth has no donor and some name donors 13 to 15, who do not exist. Gifts are
    given at hours other than midnight; days are open Monday to Friday.
    """
    config = {
        "host": migrated_db.postgres_host,
        "port": migrated_db.postgres_port,
        "database": migrated_db.postgres_db,
        "user": migrated_db.postgres_user,
        "password": migrated_db.postgres_password.get_secret_value(),
    }
    conninfo = {k if k != "database" else "dbname": v for k, v in config.items()}
    with psycopg.connect(**conninfo, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS r2 CASCADE")
        conn.execute("CREATE SCHEMA r2")
        conn.execute(
            "CREATE TABLE r2.regions (code text PRIMARY KEY, name text, lead_donor integer)"
        )
        conn.execute(
            "INSERT INTO r2.regions VALUES ('VIC', 'Victoria', 3), ('NSW', 'New South Wales', 1), "
            "('QLD', 'Queensland', 2)"
        )
        conn.execute("CREATE TABLE r2.donors (id integer PRIMARY KEY, name text, region text)")
        conn.execute(
            "INSERT INTO r2.donors SELECT n, 'Donor ' || n, (ARRAY['VIC','NSW','QLD'])[1 + n % 3] "
            "FROM generate_series(1, 12) n"
        )
        conn.execute(
            """CREATE TABLE r2.gifts (id integer PRIMARY KEY, gift_code text, donor_id integer,
                 referrer_id integer, channel text, amount numeric(10,2), gift_at timestamp)"""
        )
        conn.execute(
            """INSERT INTO r2.gifts
               SELECT n, 'G' || lpad(n::text, 3, '0'),
                      CASE WHEN n % 10 = 0 THEN NULL ELSE 1 + n % 15 END,
                      1 + (n * 7) % 12, (ARRAY['web','mail','event'])[1 + n % 3], n * 10,
                      TIMESTAMP '2026-01-01' + n * INTERVAL '1 day' + (n % 24) * INTERVAL '1 hour'
               FROM generate_series(1, 60) n"""
        )
        conn.execute("CREATE TABLE r2.days (day date PRIMARY KEY, is_open boolean)")
        conn.execute(
            """INSERT INTO r2.days
               SELECT d::date, extract(isodow FROM d) < 6
               FROM generate_series(DATE '2026-01-01', DATE '2026-04-30', INTERVAL '1 day') d"""
        )
        yield {"config": config, "conn": conn}
        conn.execute("DROP SCHEMA IF EXISTS r2 CASCADE")


class World:
    """The four tables as datasets, an admin, and editors who see VIC or everything."""

    def __init__(self, client_for: ClientFor, r2: dict[str, Any]) -> None:
        self.client_for = client_for
        self.conn = r2["conn"]
        self.admin = admin_client(client_for, **SERVER_SIGN_IN)
        self.connection = connection = connect(self.admin, "postgres", r2["config"])
        self.ids = {
            table: make_dataset(self.admin, connection, f"r2.{table}", name=table)["id"]
            for table in ("regions", "donors", "gifts", "days")
        }
        run_jobs(self.admin)
        self.vic, self.vic_id = person(
            client_for, self.admin, "vic@example.test", "editor", REGIONS, **SERVER_SIGN_IN
        )
        self.everywhere, _ = person(
            client_for, self.admin, "all@example.test", "editor", EVERYWHERE, **SERVER_SIGN_IN
        )

    def change(self, *ops: dict[str, Any], status: int = 200) -> Any:
        version = ok(self.admin.get("/api/model"))["version"]
        return ok(
            self.admin.patch("/api/model", json={"ops": list(ops), "baseVersion": version}), status
        )

    def relate(self, rid: str, rel: dict[str, Any], status: int = 200) -> Any:
        return self.change(
            {"op": "add", "path": f"/relationships/{rid}", "value": rel}, status=status
        )

    def rule(self, table: str, rules: list[dict[str, Any]], status: int = 200) -> Any:
        return self.change(
            {"op": "add", "path": f"/tables/{table}/rls", "value": rules}, status=status
        )

    def count(self, sql: str) -> int:
        return int(self.conn.execute(sql).fetchone()[0])

    def codes(self, sql: str) -> set[str]:
        return {row[0] for row in self.conn.execute(sql)}


def seen(client: TestClient, body: dict[str, Any] = COUNT, **extra: Any) -> Any:
    return query(client, body, **extra)


def rows(client: TestClient, body: dict[str, Any] = COUNT, **extra: Any) -> list[Any]:
    return seen(client, body, **extra)["rows"]  # type: ignore[no-any-return]


def codes_of(client: TestClient, **extra: Any) -> set[str]:
    return {row[0] for row in rows(client, CODES, **extra)}


VIC_GIFTS = (
    "SELECT {what} FROM r2.gifts g JOIN r2.donors d ON d.id = g.donor_id WHERE d.region = 'VIC'"
)


@pytest.fixture
def world(client_for: ClientFor, r2: dict[str, Any]) -> World:
    return World(client_for, r2)


def test_a_rule_on_donors_hides_other_donors_gifts_and_gifts_of_no_or_unknown_donor(
    world: World,
) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.rule("donors", DONORS_RULE)
    vic = world.count(VIC_GIFTS.format(what="count(*)"))
    assert 0 < vic < 60
    assert rows(world.vic) == [[vic]]
    shown = codes_of(world.vic)
    assert shown == world.codes(VIC_GIFTS.format(what="g.gift_code"))
    no_donor = world.codes("SELECT gift_code FROM r2.gifts WHERE donor_id IS NULL")
    unknown = world.codes("SELECT gift_code FROM r2.gifts WHERE donor_id > 12")
    assert no_donor and unknown and not shown & (no_donor | unknown)

    # Seeing every region shows every donor's gifts, but still no gift without a donor
    # one may see: a rule on donors keeps only gifts whose donor passes it.
    known = world.count("SELECT count(*) FROM r2.gifts g JOIN r2.donors d ON d.id = g.donor_id")
    assert rows(world.everywhere) == [[known]] and known == 60 - len(no_donor | unknown)
    assert seen(world.vic)["rlsBypassed"] is False

    # Gifts' fields with donors' fields: the same gifts, grouped by their donor's region.
    by_region = {**COUNT, "dimensions": ["donors.region"]}
    assert rows(world.vic, by_region) == [["VIC", vic]]

    # An admin previewing as someone sees what they see.
    assert rows(world.admin, previewAs={"role": "editor", "attributes": REGIONS}) == [[vic]]


def test_two_steps_a_rule_on_regions_reaches_gifts_through_donors(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.relate("donors__regions", DONORS_REGIONS)
    world.rule("regions", REGIONS_RULE)
    vic = world.count(VIC_GIFTS.format(what="count(*)"))
    assert rows(world.vic) == [[vic]]
    assert codes_of(world.vic) == world.codes(VIC_GIFTS.format(what="g.gift_code"))
    donors = {"dataset": "donors", "dimensions": ["donors.id"], "measures": ["donors.row_count"]}
    assert {row[0] for row in rows(world.vic, donors)} == {3, 6, 9, 12}


def test_security_both_ways_lets_a_rule_on_gifts_filter_donors(world: World) -> None:
    channels = [{"filter": "channel IN {{ user.attributes.channels }}"}]
    world.rule("gifts", channels)
    web, _ = person(
        world.client_for, world.admin, "web@example.test", "editor", {"channels": ["web"]},
        **SERVER_SIGN_IN,
    )  # fmt: skip
    donors = {"dataset": "donors", "dimensions": ["donors.id"], "measures": ["donors.row_count"]}

    # One way (the default): a rule on the many side does not reach the one side.
    world.relate("gifts__donors", GIFTS_DONORS)
    assert len(rows(web, donors)) == 12
    assert rows(web) == [[world.count("SELECT count(*) FROM r2.gifts WHERE channel = 'web'")]]

    # Both ways: donors without a web gift are hidden too.
    both = {**GIFTS_DONORS, "crossFilter": "both", "securityFilter": "both"}
    world.change({"op": "replace", "path": "/relationships/gifts__donors", "value": both})
    with_web = world.codes(
        "SELECT DISTINCT d.id::text FROM r2.donors d JOIN r2.gifts g ON g.donor_id = d.id "
        "WHERE g.channel = 'web'"
    )
    assert 0 < len(with_web) < 12
    assert {str(row[0]) for row in rows(web, donors)} == with_web

    # Both ways needs filters both ways.
    wrong = {**GIFTS_DONORS, "securityFilter": "both"}
    refused = world.change(
        {"op": "replace", "path": "/relationships/gifts__donors", "value": wrong}, status=422
    )
    assert any("crossFilter" in f"{p['path']} {p['message']}" for p in refused["error"]["details"])


def test_inactive_relationships_carry_nothing_and_a_measure_over_one_hides_nothing_more(
    world: World,
) -> None:
    world.rule("donors", DONORS_RULE)
    world.relate("gifts__donors", {**GIFTS_DONORS, "active": False})
    assert rows(world.vic) == [[60]]  # an inactive relationship carries no rule

    # With the active one too, a measure that names the inactive one (gifts by the
    # region of their referrer) counts no gift the active one hides.
    referrers = {"from": end("gifts", "referrer_id"), "to": end("donors", "id"), "active": False}
    world.change(
        {"op": "replace", "path": "/relationships/gifts__donors", "value": GIFTS_DONORS},
        {"op": "add", "path": "/relationships/gifts__donors_2", "value": referrers},
        {"op": "add", "path": "/tables/gifts/measures/referred",
         "value": {"agg": "count", "relationship": "gifts__donors_2"}},
    )  # fmt: skip
    vic = world.count(VIC_GIFTS.format(what="count(*)"))
    by_referrer = {
        "dataset": "gifts",
        "dimensions": ["donors.region"],
        "measures": ["gifts.referred"],
    }
    found = rows(world.vic, by_referrer)
    assert sum(count for _, count in found) == vic
    assert {region for region, _ in found} <= {"VIC", None}  # referrers outside VIC are hidden
    expected = world.count(
        "SELECT count(*) FROM r2.gifts g JOIN r2.donors d ON d.id = g.donor_id "
        "JOIN r2.donors r ON r.id = g.referrer_id WHERE d.region = 'VIC' AND r.region = 'VIC'"
    )
    assert dict(found).get("VIC", 0) == expected


def test_admins_are_exempt_and_say_so_and_share_links_never_are(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.rule("donors", DONORS_RULE)
    vic = world.count(VIC_GIFTS.format(what="count(*)"))
    exempt = seen(world.admin)
    assert exempt["rows"] == [[60]] and exempt["rlsBypassed"] is True

    kpi = {"id": "k", "visual": "kpi", "position": {"x": 0, "y": 0, "w": 3, "h": 3},
           "dataset": "gifts", "bindings": {"value": ["gifts.row_count"]}}  # fmt: skip
    spec = {"specVersion": 1, "title": "Gifts", "layout": {"columns": 12, "rowHeight": 40},
            "filters": [], "items": [kpi]}  # fmt: skip
    made = world.admin.post("/api/dashboards", json={"title": "Gifts", "spec": spec})
    dashboard = ok(made, 201)["id"]
    data = ok(world.admin.post(f"/api/dashboards/{dashboard}/data", json={"itemId": "k"}))
    assert data["rows"] == [[60]] and data["rlsBypassed"] is True
    ok(world.admin.post(f"/api/dashboards/{dashboard}/publish"))

    anonymous = world.client_for(**SERVER_SIGN_IN)

    def link_data(attributes: dict[str, Any] | None) -> Any:
        body = {"attributes": attributes} if attributes is not None else {}
        link = ok(world.admin.post(f"/api/dashboards/{dashboard}/links", json=body), 201)
        token = link["url"].rsplit("/", 1)[1]
        return ok(anonymous.post(f"/api/public/links/{token}/data", json={"itemId": "k"}))

    # An admin's link is not exempt: with no attributes the rule shows nothing.
    nothing = link_data(None)
    assert nothing["rows"] == [[0]] and not nothing.get("rlsBypassed")
    assert link_data(REGIONS)["rows"] == [[vic]]


def test_previews_profiles_filter_options_and_samples_follow_it(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.rule("donors", DONORS_RULE)
    vic = world.count(VIC_GIFTS.format(what="count(*)"))
    vic_codes = world.codes(VIC_GIFTS.format(what="g.gift_code"))
    vic_donors = {3, 6, 9, 12}

    preview = ok(world.vic.get(f"/api/datasets/{world.ids['gifts']}/preview?limit=100"))
    code = [c["name"] for c in preview["columns"]].index("gift_code")
    assert {row[code] for row in preview["rows"]} == vic_codes
    assert preview["rowCount"] is None and preview["rlsApplied"] is True
    # A table no rule reaches is read as it is.
    regions = ok(world.vic.get(f"/api/datasets/{world.ids['regions']}/preview"))
    assert regions["rowCount"] == 3 and regions["rlsApplied"] is False

    # The agent's tools read the same rows.
    token = ok(world.vic.post("/api/tokens", json={"name": "agent", "scopes": ["read"]}), 201)[
        "token"
    ]
    agent = Mcp(world.vic, token)
    shown = agent.call("preview_dataset", dataset="gifts", limit=100)
    assert shown["rowsShown"] == vic and shown["rowCount"] is None
    profile = agent.call("profile_column", dataset="gifts", column="donor_id")
    assert profile["rows"] == vic
    assert {item["value"] for item in profile["mostCommon"]} <= vic_donors
    described = agent.call("describe_dataset", dataset="gifts")
    samples = {d["ref"]: d.get("sampleValues") for d in described["dimensions"]}
    assert set(samples["gifts.gift_code"] or []) <= vic_codes and samples["gifts.gift_code"]
    assert set(samples["gifts.donor_id"] or []) <= vic_donors
    assert (
        described["rowCount"] is None
        and "row-level security" in described["sampleValuesNote"].lower()
    )

    # Filter options list only values of rows one may see.
    item = {"id": "t", "visual": "table", "position": {"x": 0, "y": 0, "w": 12, "h": 6},
            "dataset": "gifts", "bindings": {"columns": ["gifts.gift_code", "gifts.row_count"]}}  # fmt: skip
    spec = {"specVersion": 1, "title": "Codes", "layout": {"columns": 12, "rowHeight": 40},
            "filters": [{"id": "f_code", "type": "multiSelect", "field": "gifts.gift_code"}],
            "items": [item]}  # fmt: skip
    dashboard = ok(world.admin.post("/api/dashboards", json={"title": "Codes", "spec": spec}), 201)[
        "id"
    ]
    shares = [{"grantee": f"user:{world.vic_id}", "role": "editor"}]
    ok(world.admin.put(f"/api/dashboards/{dashboard}/access", json={"shares": shares}))
    options = ok(
        world.vic.post(f"/api/dashboards/{dashboard}/filter-options", json={"filterId": "f_code"})
    )
    assert set(options["values"]) == vic_codes
    everything = ok(
        world.admin.post(f"/api/dashboards/{dashboard}/filter-options", json={"filterId": "f_code"})
    )
    assert len(everything["values"]) == 60


def test_someone_who_may_not_use_donors_still_gets_filtered_gifts(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.rule("donors", DONORS_RULE)
    ok(world.admin.put(f"/api/datasets/{world.ids['donors']}/access", json={"restricted": True}))
    vic = world.count(VIC_GIFTS.format(what="count(*)"))
    assert rows(world.vic) == [[vic]]  # no access error: donors is read only to filter
    by_region = {**COUNT, "dimensions": ["donors.region"]}
    # Asking for its fields is what needs access.
    refused = query(world.vic, by_region, status=403)
    assert refused["error"]["code"] == "dataset_forbidden"
    # The model they see has no donors, nor the relationship to it.
    model = ok(world.vic.get("/api/model"))
    assert "donors" not in model["document"]["tables"] and model["document"]["relationships"] == {}


def test_a_loop_through_a_table_with_rules_is_refused(world: World) -> None:
    world.relate("gifts__donors", GIFTS_DONORS)
    world.relate("donors__regions", DONORS_REGIONS)
    lead = {"from": end("regions", "lead_donor"), "to": end("donors", "id")}

    # Without rules the loop is allowed.
    world.relate("regions__donors", lead)
    world.change({"op": "remove", "path": "/relationships/regions__donors"})

    world.rule("regions", REGIONS_RULE)
    refused = world.relate("regions__donors", lead, status=422)
    assert any(
        "form a loop that row-level security" in p["message"] for p in refused["error"]["details"]
    )
    # Dragging the same fields together offers it inactive, and says why.
    offer = ok(
        world.admin.post("/api/model/key-check", json={"from": lead["from"], "to": lead["to"]})
    )
    assert offer["relationship"]["active"] is False and "loop" in offer["inactiveBecause"]
    world.relate("regions__donors", offer["relationship"])  # inactive, it may be saved

    # A rule added to a loop that exists is refused the same way.
    world.change({"op": "remove", "path": "/tables/regions/rls"})
    world.change(
        {
            "op": "replace",
            "path": "/relationships/regions__donors",
            "value": {**lead, "active": True},
        }
    )
    refused = world.rule("donors", DONORS_RULE, status=422)
    assert any("loop" in p["message"] for p in refused["error"]["details"])


def test_a_date_table_rule_keeps_timestamps_of_its_visible_days(world: World) -> None:
    world.relate("gifts__days", {"from": end("gifts", "gift_at"), "to": end("days", "day")})
    world.rule("days", [{"filter": "is_open = true"}])
    open_days = world.count(
        "SELECT count(*) FROM r2.gifts g JOIN r2.days d ON d.day = g.gift_at::date WHERE d.is_open"
    )
    midnight = world.count("SELECT count(*) FROM r2.gifts WHERE gift_at::time = '00:00'")
    assert open_days > midnight  # matched on the day, not at midnight
    assert rows(world.vic) == [[open_days]]
