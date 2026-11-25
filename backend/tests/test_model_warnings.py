"""Warnings on a semantic model: a join key that is on more than one row, and
row-level security on a joined dataset that does not limit the base dataset."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opsis.errors import ApiError
from opsis.query.engine import QueryError
from tests.test_auth import admin_client, person
from tests.test_ingestion import connect, make_dataset, ok, refresh, run_jobs
from tests.test_mcp import Mcp
from tests.test_query_api import as_dicts, load, query
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]

TO_FUNDS = {"to": "funds", "on": {"fund": "fund"}, "relationship": "many_to_one"}
TO_DONORS = {
    "to": "donors",
    "on": {"donor_id": "id"},
    "relationship": "many_to_one",
    "type": "inner",
}
FUNDS_TO_CATEGORIES = {"to": "categories", "on": {"code": "code"}, "relationship": "many_to_one"}
REGION_RULE = [{"filter": "region IN {{ user.attributes.regions }}"}]


@pytest.fixture
def lookups(source: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    """gifts and donors, plus funds (looked up by name) and categories (by the funds' code)."""
    conn = source["conn"]
    conn.execute("CREATE TABLE src.funds (fund text, category text, code text)")
    conn.execute(
        """INSERT INTO src.funds VALUES ('Library','Facilities','F'), ('Sport','Community','C'),
           ('Scholarships','Students','S'), ('Research','Research','R')"""
    )
    conn.execute("CREATE TABLE src.categories (code text, area text)")
    conn.execute(
        "INSERT INTO src.categories VALUES ('F','North'), ('C','South'), ('S','East'), ('R','West')"
    )
    return source


def load_all(client: TestClient, source: dict[str, Any]) -> dict[str, str]:  # noqa: F811
    ids = load(client, source)
    connection = connect(client, "postgres", source["config"], name="lookups")
    for table in ("funds", "categories"):
        ids[table] = make_dataset(client, connection, f"src.{table}", name=table)["id"]
    run_jobs(client)
    return ids


def model_of(*fields: str, **extra: Any) -> dict[str, Any]:
    """A model with these text columns as dimensions, and a row count."""
    return {
        "dimensions": {name: {"column": name, "type": "string"} for name in fields},
        "measures": {"row_count": {"agg": "count"}},
        **extra,
    }


def gifts_model(*joins: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return model_of("fund", "region", joins=list(joins), **extra)


def save(client: TestClient, dataset: str, definition: dict[str, Any]) -> dict[str, Any]:
    return ok(client.put(f"/api/models/{dataset}", json={"definition": definition}))  # type: ignore[no-any-return]


def validate(client: TestClient, dataset: str, definition: dict[str, Any]) -> dict[str, Any]:
    body = {"definition": definition}
    return ok(client.post(f"/api/models/{dataset}/validate", json=body))  # type: ignore[no-any-return]


def viewed(client: TestClient, dataset: str) -> list[dict[str, Any]]:
    return ok(client.get(f"/api/models/{dataset}"))["warnings"]  # type: ignore[no-any-return]


def counts_by(client: TestClient, field: str) -> dict[str, int]:
    found = query(
        client, {"dataset": "gifts", "measures": ["gifts.row_count"], "dimensions": [field]}
    )
    return {row[field]: row["gifts.row_count"] for row in as_dicts(found)}


def test_a_repeated_join_key_warns_and_is_checked_again_after_a_refresh(
    client_for: ClientFor, lookups: dict[str, Any]
) -> None:
    client = client_for()
    ids = load_all(client, lookups)
    definition = gifts_model(TO_FUNDS)
    assert validate(client, "gifts", definition) == {"valid": True, "problems": [], "warnings": []}
    assert save(client, "gifts", definition)["warnings"] == []

    # The lookup now lists Library twice and Sport three times. Rows with an empty
    # key match nothing, however often they appear.
    conn = lookups["conn"]
    conn.execute(
        """INSERT INTO src.funds VALUES ('Library','Buildings','F'), ('Sport','Clubs','C'),
           ('Sport','Teams','C'), (NULL,'None','N'), (NULL,'None','N')"""
    )
    refresh(client, ids["funds"])
    # Why it matters: each Library gift is now counted twice, each Sport gift three times.
    assert counts_by(client, "funds.fund") == {
        "Library": 1000,
        "Sport": 1500,
        "Scholarships": 500,
        "Research": 500,
    }

    [warning] = viewed(client, "gifts")
    assert {
        "code": "duplicate_join_key",
        "path": "/joins/0",
        "dataset": "funds",
        "through": None,
        "key": ["fund"],
        "repeatedValues": 2,
        "examples": [
            {"values": {"fund": "Sport"}, "rows": 3},
            {"values": {"fund": "Library"}, "rows": 2},
        ],
        "examplesHidden": False,
    }.items() <= warning.items()
    for part in (
        "The join to 'funds' is declared many_to_one, but its key 'fund' is not unique",
        "2 values are on more than one row, for example 'Sport' (3 rows), 'Library' (2 rows).",
        "A row of 'gifts' that matches a repeated value is counted once for every row",
        "totals and counts are too high in queries that use fields from 'funds'",
    ):
        assert part in warning["message"]

    # A warning, not an error: Check passes, and Save and Restore go through.
    assert validate(client, "gifts", definition) == {
        "valid": True,
        "problems": [],
        "warnings": [warning],
    }
    saved = save(client, "gifts", {**definition, "label": "All gifts"})
    assert (saved["version"], saved["warnings"]) == (3, [warning])
    restored = ok(client.post("/api/models/gifts/versions/2/restore"))
    assert (restored["version"], restored["warnings"]) == (4, [warning])

    conn.execute("DELETE FROM src.funds WHERE category IN ('Buildings','Clubs','Teams')")
    refresh(client, ids["funds"])
    assert viewed(client, "gifts") == []

    # A key column that a refresh took away is a problem for Check, not a warning.
    conn.execute("ALTER TABLE src.funds RENAME COLUMN fund TO name")
    refresh(client, ids["funds"])
    assert viewed(client, "gifts") == []
    broken = validate(client, "gifts", definition)
    assert (broken["valid"], broken["warnings"]) == (False, [])
    assert broken["problems"][0]["path"] == "/joins/0/on/fund"


def test_composite_keys_five_examples_and_only_the_joins_queries_use(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    conn = source["conn"]
    conn.execute('CREATE TABLE src.targets ("Fund ""name""" text, region text, goal integer)')
    # Seven (fund, region) pairs are on two rows or more; one pair is on one row,
    # and pairs with an empty region match nothing.
    conn.execute(
        """INSERT INTO src.targets
           SELECT f, r, 1 FROM unnest(ARRAY['Library','Sport','Scholarships']) f,
                  unnest(ARRAY['VIC','NSW','QLD']) r, generate_series(1, 2)
           WHERE NOT (f = 'Scholarships' AND r <> 'VIC')"""
    )
    conn.execute(
        """INSERT INTO src.targets VALUES ('Sport','VIC',2), ('Research','VIC',1),
           ('Research',NULL,1), ('Research',NULL,1)"""
    )
    client = client_for()
    load(client, source)
    connection = connect(client, "postgres", source["config"], name="lookups")
    make_dataset(client, connection, "src.targets", name="targets")
    run_jobs(client)

    join = {
        "to": "targets",
        "on": {"fund": 'Fund "name"', "region": "region"},
        "relationship": "one_to_one",
    }
    [warning] = validate(client, "gifts", gifts_model(join))["warnings"]
    assert (warning["key"], warning["repeatedValues"]) == (['Fund "name"', "region"], 7)
    assert [
        (e["values"]['Fund "name"'], e["values"]["region"], e["rows"]) for e in warning["examples"]
    ] == [
        ("Sport", "VIC", 3),
        ("Library", "NSW", 2),
        ("Library", "QLD", 2),
        ("Library", "VIC", 2),
        ("Scholarships", "VIC", 2),
    ]
    assert "declared one_to_one" in warning["message"]
    assert "its key ('Fund \"name\"', 'region') is not unique" in warning["message"]
    assert (
        "7 values are on more than one row, for example ('Sport', 'VIC') (3 rows)"
        in warning["message"]
    )

    # Queries cannot use a one_to_many join, so it cannot double anything.
    many = {**join, "relationship": "one_to_many"}
    assert validate(client, "gifts", gifts_model(many))["warnings"] == []


def test_a_repeated_key_two_joins_away_warns_the_models_that_reach_it(
    client_for: ClientFor, lookups: dict[str, Any]
) -> None:
    client = admin_client(client_for)  # restricting a dataset needs sign-in
    lookups["conn"].execute("INSERT INTO src.categories VALUES ('F','Upper')")
    ids = load_all(client, lookups)
    funds = model_of("fund", "category", "code", joins=[FUNDS_TO_CATEGORIES])
    [own] = save(client, "funds", funds)["warnings"]
    assert (own["path"], own["dataset"], own["through"]) == ("/joins/0", "categories", None)

    save(client, "categories", model_of("code", "area"))
    [far] = save(client, "gifts", gifts_model(TO_FUNDS))["warnings"]
    assert (far["path"], far["dataset"], far["through"]) == ("/joins/0", "categories", "funds")
    assert far["repeatedValues"] == 1
    assert far["examples"] == [{"values": {"code": "F"}, "rows": 2}]
    for part in (
        "The join from 'funds' to 'categories' is declared many_to_one",
        "A row of 'gifts' that leads through 'funds' to a repeated value",
        "(the join is in the model of 'funds')",
    ):
        assert part in far["message"]
    assert counts_by(client, "categories.code")["F"] == 1000

    # A path never goes through a restricted dataset, so nothing can be doubled.
    access = f"/api/datasets/{ids['funds']}/access"
    ok(client.put(access, json={"restricted": True, "grants": []}))
    assert viewed(client, "gifts") == []


def test_rules_on_a_joined_dataset_do_not_limit_the_base_and_that_is_said(
    client_for: ClientFor, lookups: dict[str, Any]
) -> None:
    client = client_for()
    load_all(client, lookups)
    assert save(client, "donors", model_of("region", rls=REGION_RULE))["warnings"] == []

    [warning] = validate(client, "gifts", gifts_model(TO_DONORS))["warnings"]
    assert {
        "code": "joined_rules_not_applied",
        "path": "/joins/0",
        "dataset": "donors",
        "through": None,
    }.items() <= warning.items()
    for part in (
        "'donors' has row-level security but 'gifts' has none",
        "a query that uses no field from 'donors' returns all of them, even when the join is inner",
        "add a rule to its model on one of its own columns",
    ):
        assert part in warning["message"]
    assert save(client, "gifts", gifts_model(TO_DONORS))["warnings"] == [warning]
    assert viewed(client, "gifts") == [warning]

    # Why it matters: a viewer who may see no donor at all still sees every gift.
    seen = query(
        client,
        {"dataset": "gifts", "measures": ["gifts.row_count"]},
        previewAs={"role": "viewer", "attributes": {}},
    )
    assert seen["rows"] == [[2000]]

    # With a rule of its own the base model is not flagged.
    assert validate(client, "gifts", gifts_model(TO_DONORS, rls=REGION_RULE))["warnings"] == []
    # A join that queries cannot use does not protect this dataset either.
    many = {**TO_DONORS, "relationship": "one_to_many"}
    [flagged] = validate(client, "gifts", gifts_model(many))["warnings"]
    assert flagged["code"] == "joined_rules_not_applied"

    # Two joins away, behind a dataset that has no rules.
    area_rule = [{"filter": "area = {{ user.attributes.area }}"}]
    save(client, "categories", model_of("code", "area", rls=area_rule))
    funds = model_of("fund", "code", joins=[FUNDS_TO_CATEGORIES])
    [between] = save(client, "funds", funds)["warnings"]
    assert between["dataset"] == "categories"
    [far] = validate(client, "gifts", gifts_model(TO_FUNDS))["warnings"]
    assert (far["path"], far["dataset"], far["through"]) == ("/joins/0", "categories", "funds")
    assert "'categories' (reached through 'funds') has row-level security" in far["message"]


def test_examples_are_hidden_from_people_who_may_not_see_the_data(
    client_for: ClientFor, lookups: dict[str, Any]
) -> None:
    admin = admin_client(client_for)
    ids = load_all(admin, lookups)
    lookups["conn"].execute("INSERT INTO src.funds VALUES ('Library','Buildings','F')")
    refresh(admin, ids["funds"])
    definition = gifts_model(TO_FUNDS)
    save(admin, "gifts", definition)
    editor, editor_id = person(client_for, admin, "editor@example.test", "editor")
    token = ok(editor.post("/api/tokens", json={"name": "agent", "scopes": ["read", "edit"]}), 201)
    agent = Mcp(editor, token["token"])
    shown = [{"values": {"fund": "Library"}, "rows": 2}]

    def repeated(warnings: list[dict[str, Any]]) -> dict[str, Any]:
        [found] = [w for w in warnings if w["code"] == "duplicate_join_key"]
        return found

    def check_hidden() -> None:
        """The editor, everywhere: the page, Check, Save and an agent."""
        for warnings in (
            viewed(editor, "gifts"),
            validate(editor, "gifts", definition)["warnings"],
            save(editor, "gifts", definition)["warnings"],
            agent.call("get_semantic_model", dataset="gifts")["warnings"],
            agent.call("validate_model", dataset="gifts", definition=definition)["warnings"],
        ):
            warning = repeated(warnings)
            assert (warning["examples"], warning["examplesHidden"]) == ([], True)
            assert warning["repeatedValues"] == 1
            assert "Library" not in str(warning)
            assert (
                "Examples are hidden: you may not see all of the data in 'funds'."
                in warning["message"]
            )
        # The admin sees them: exempt from the rule, and allowed any dataset.
        assert repeated(viewed(admin, "gifts"))["examples"] == shown

    assert repeated(viewed(editor, "gifts"))["examples"] == shown

    # Row-level security on the joined dataset that applies to editors.
    rules = [{"filter": "category IN {{ user.attributes.categories }}"}]
    save(admin, "funds", model_of("fund", rls=rules))
    check_hidden()

    # A restricted joined dataset that the editor may not use.
    save(admin, "funds", model_of("fund"))
    assert repeated(viewed(editor, "gifts"))["examples"] == shown
    access = f"/api/datasets/{ids['funds']}/access"
    ok(admin.put(access, json={"restricted": True, "grants": []}))
    check_hidden()
    ok(admin.put(access, json={"restricted": True, "grants": [f"user:{editor_id}"]}))
    assert repeated(viewed(editor, "gifts"))["examples"] == shown


def test_agents_are_given_the_warnings(client_for: ClientFor, lookups: dict[str, Any]) -> None:
    client = client_for()
    lookups["conn"].execute("INSERT INTO src.funds VALUES ('Library','Buildings','F')")
    load_all(client, lookups)
    mcp = Mcp(client)
    model = mcp.call("get_semantic_model", dataset="gifts")
    assert model["warnings"] == []

    add = {"op": "add", "path": "/joins", "value": [TO_FUNDS]}
    patched = mcp.call(
        "patch_semantic_model", dataset="gifts", base_version=model["version"], ops=[add]
    )
    assert patched["saved"] and patched["version"] == model["version"] + 1
    [warning] = patched["warnings"]
    assert (warning["code"], warning["repeatedValues"]) == ("duplicate_join_key", 1)
    assert "tell the user" in patched["note"]
    after = mcp.call("get_semantic_model", dataset="gifts")
    assert after["warnings"] == [warning]
    checked = mcp.call("validate_model", dataset="gifts", definition=after["definition"])
    assert (checked["valid"], checked["warnings"]) == (True, [warning])

    wrong = {"joins": [{"to": "funds", "on": {"nope": "fund"}}]}
    invalid = mcp.call("validate_model", dataset="gifts", definition=wrong)
    assert (invalid["valid"], invalid["warnings"]) == (False, [])

    # A save without warnings keeps the plain note.
    remove = {"op": "remove", "path": "/joins"}
    clean = mcp.call(
        "patch_semantic_model", dataset="gifts", base_version=patched["version"], ops=[remove]
    )
    assert clean["warnings"] == [] and clean["note"].startswith("Dashboards that use")


@pytest.mark.parametrize(
    "failure", [ApiError(503, "busy", "The server is busy."), QueryError("no such file")]
)
def test_a_key_that_cannot_be_checked_is_said_and_never_blocks_a_save(
    client_for: ClientFor,
    lookups: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    client = client_for()
    load_all(client, lookups)

    def fail(*_: Any, **__: Any) -> None:
        raise failure

    app: Any = client.app
    monkeypatch.setattr(app.state.query_runtime, "execute", fail)
    saved = save(client, "gifts", gifts_model(TO_FUNDS))
    [warning] = saved["warnings"]
    assert (saved["version"], warning["code"], warning["key"]) == (
        2,
        "join_key_not_checked",
        ["fund"],
    )
    assert "Whether the key 'fund' of 'funds' is unique could not be checked" in warning["message"]
    assert viewed(client, "gifts") == [warning]
