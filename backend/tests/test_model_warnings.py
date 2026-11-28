"""Warnings on the workspace model (V2_DESIGN 1.2): a relationship whose key is on more
than one row of its one side, a relationship into a restricted table, and what the
warnings show to whom. The v1 view of one table shows its joins' warnings at
/joins/<n> with v1's codes; the Model view shows each once, on its relationship."""

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


def in_model_view(client: TestClient) -> list[dict[str, Any]]:
    return ok(client.get("/api/model"))["warnings"]  # type: ignore[no-any-return]


def counts_by(client: TestClient, field: str) -> dict[str, int]:
    found = query(
        client, {"dataset": "gifts", "measures": ["gifts.row_count"], "dimensions": [field]}
    )
    return {row[field]: row["gifts.row_count"] for row in as_dicts(found)}


def test_a_repeated_key_warns_and_is_checked_again_after_a_refresh(
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
        "relationship": "gifts__funds",
        "dataset": "funds",
        "key": ["fund"],
        "repeatedValues": 2,
        "examples": [
            {"values": {"fund": "Sport"}, "rows": 3},
            {"values": {"fund": "Library"}, "rows": 2},
        ],
        "examplesHidden": False,
    }.items() <= warning.items()
    for part in (
        "The relationship gifts (fund) to funds (fund) is many-to-one, but its key 'fund' is "
        "not unique in 'funds'",
        "2 values are on more than one row, for example 'Sport' (3 rows), 'Library' (2 rows).",
        "A row of 'gifts' that matches a repeated value is counted once for every row",
        "totals and counts are too high in queries that use fields from 'funds'",
    ):
        assert part in warning["message"]
    # The Model view shows it once, on the relationship, with the workspace code.
    [shown] = in_model_view(client)
    assert (shown["code"], shown["path"]) == ("duplicate_key", "/relationships/gifts__funds")
    assert shown["message"] == warning["message"]
    assert viewed(client, "funds") == []  # the relationship is one of gifts' joins

    # A warning, not an error: Check passes, and Save and Restore go through.
    assert validate(client, "gifts", definition) == {
        "valid": True,
        "problems": [],
        "warnings": [warning],
    }
    saved = save(client, "gifts", {**definition, "label": "All gifts"})
    assert (saved["version"], saved["warnings"]) == (2, [warning])
    restored = ok(client.post("/api/models/gifts/versions/1/restore"))
    assert (restored["version"], restored["warnings"]) == (3, [warning])

    conn.execute("DELETE FROM src.funds WHERE category IN ('Buildings','Clubs','Teams')")
    refresh(client, ids["funds"])
    assert viewed(client, "gifts") == [] and in_model_view(client) == []

    # A key column that a refresh took away is a problem for Check, not a warning.
    conn.execute("ALTER TABLE src.funds RENAME COLUMN fund TO name")
    refresh(client, ids["funds"])
    assert viewed(client, "gifts") == []
    broken = validate(client, "gifts", definition)
    assert (broken["valid"], broken["warnings"]) == (False, [])
    assert broken["problems"][0]["path"] == "/joins/0/on/fund"


def test_composite_keys_five_examples_and_inactive_relationships_too(
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
    assert (
        "is one-to-one, but its key ('Fund \"name\"', 'region') is not unique" in warning["message"]
    )
    assert (
        "7 values are on more than one row, for example ('Sport', 'VIC') (3 rows)"
        in warning["message"]
    )

    # A one_to_many join becomes an inactive relationship the other way round, from
    # targets to gifts. A measure may name it, so its one side is checked too: gifts
    # holds every (fund, region) pair on many rows.
    many = {**join, "relationship": "one_to_many"}
    [inactive] = validate(client, "gifts", gifts_model(many))["warnings"]
    assert (inactive["dataset"], inactive["key"], inactive["repeatedValues"]) == (
        "gifts",
        ["fund", "region"],
        12,
    )
    assert inactive["relationship"] == "targets__gifts"


def test_a_relationship_further_away_is_warned_once_on_itself(
    client_for: ClientFor, lookups: dict[str, Any]
) -> None:
    client = admin_client(client_for)  # restricting a dataset needs sign-in
    lookups["conn"].execute("INSERT INTO src.categories VALUES ('F','Upper')")
    ids = load_all(client, lookups)
    funds = model_of("fund", "category", "code", joins=[FUNDS_TO_CATEGORIES])
    [own] = save(client, "funds", funds)["warnings"]
    assert (own["path"], own["dataset"], own["relationship"]) == (
        "/joins/0",
        "categories",
        "funds__categories",
    )
    save(client, "categories", model_of("code", "area"))
    # gifts reaches categories through funds. The repeated key is the relationship's,
    # not gifts': it is shown once, on funds to categories.
    assert save(client, "gifts", gifts_model(TO_FUNDS))["warnings"] == []
    [far] = in_model_view(client)
    assert (far["path"], far["repeatedValues"]) == ("/relationships/funds__categories", 1)
    assert far["examples"] == [{"values": {"code": "F"}, "rows": 2}]
    assert counts_by(client, "categories.code")["F"] == 1000  # why it matters

    # A relationship into a restricted table says what that means, and no path goes
    # through one, so gifts no longer reaches categories at all.
    ok(client.put(f"/api/datasets/{ids['funds']}/access", json={"restricted": True, "grants": []}))
    restricted = {
        w["path"] for w in in_model_view(client) if w["code"] == "relationship_to_restricted"
    }
    assert restricted == {"/relationships/gifts__funds", "/relationships/funds__categories"}
    unreachable = client.post(
        "/api/query",
        json={
            "query": {
                "dataset": "gifts",
                "measures": ["gifts.row_count"],
                "dimensions": ["categories.code"],
            }
        },
    )
    assert unreachable.status_code == 422


def test_rules_on_a_related_table_filter_it_so_nothing_needs_saying(
    client_for: ClientFor, lookups: dict[str, Any]
) -> None:
    """v1 warned that a rule on a joined dataset did not limit the base dataset
    (joined_rules_not_applied). In v2 it does (R2), and the warning is gone."""
    client = client_for()
    load_all(client, lookups)
    assert save(client, "donors", model_of("region", rls=REGION_RULE))["warnings"] == []
    assert save(client, "gifts", gifts_model(TO_DONORS))["warnings"] == []
    assert in_model_view(client) == []
    # A viewer who may see no donor at all sees no gift either.
    seen = query(
        client,
        {"dataset": "gifts", "measures": ["gifts.row_count"]},
        previewAs={"role": "viewer", "attributes": {}},
    )
    assert seen["rows"] == [[0]]


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
        [found] = [w for w in warnings if w["code"] in ("duplicate_join_key", "duplicate_key")]
        return found

    # Row-level security on the joined dataset that applies to editors.
    assert repeated(viewed(editor, "gifts"))["examples"] == shown
    rules = [{"filter": "category IN {{ user.attributes.categories }}"}]
    save(admin, "funds", model_of("fund", rls=rules))
    for warnings in (  # the editor, everywhere: the pages, Check, Save and an agent
        viewed(editor, "gifts"),
        in_model_view(editor),
        validate(editor, "gifts", definition)["warnings"],
        save(editor, "gifts", definition)["warnings"],
        agent.call("get_semantic_model", dataset="gifts")["warnings"],
        agent.call("validate_model", dataset="gifts", definition=definition)["warnings"],
        agent.call("get_workspace_model")["warnings"],
    ):
        warning = repeated(warnings)
        assert (warning["examples"], warning["examplesHidden"]) == ([], True)
        assert warning["repeatedValues"] == 1
        assert "Library" not in str(warning)
        assert (
            "Examples are hidden: you may not see all of the data in 'funds'." in warning["message"]
        )
    # The admin sees them: exempt from the rule, and allowed any dataset.
    assert repeated(viewed(admin, "gifts"))["examples"] == shown

    # A restricted table the editor may not use: they do not see the relationship at
    # all, nor its warnings, and saving gifts without it keeps it.
    save(admin, "funds", model_of("fund"))
    assert repeated(viewed(editor, "gifts"))["examples"] == shown
    access = f"/api/datasets/{ids['funds']}/access"
    ok(admin.put(access, json={"restricted": True, "grants": []}))
    hidden = ok(editor.get("/api/models/gifts"))
    assert hidden["definition"].get("joins", []) == [] and hidden["warnings"] == []
    assert "funds" not in str(in_model_view(editor))
    assert "funds" not in str(agent.call("get_workspace_model"))
    save(editor, "gifts", model_of("fund", "region", label="Gifts"))
    assert "gifts__funds" in ok(admin.get("/api/model"))["document"]["relationships"]
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
        1,
        "join_key_not_checked",
        ["fund"],
    )
    assert "Whether the key 'fund' of 'funds' is unique could not be checked" in warning["message"]
    assert viewed(client, "gifts") == [warning]
