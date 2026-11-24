"""Forgiving agent input (decided at the M7 review): a single value where a list
is expected is read as a list of one by the agent tools, stored in the strict
form, and logged at debug level. The REST API stays strict."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.db.models import DashboardVersion
from opsis.mcp import lenient
from tests.test_mcp import Mcp, prepared, spec_with
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
SPEC_DIR = Path("/app/spec")

LINE = {
    "id": "line_month",
    "visual": "line",
    "title": "Amount by month",
    "position": {"x": 0, "y": 0, "w": 6, "h": 6},
    "dataset": "gifts",
    "bindings": {"x": {"field": "gifts.gift_date", "grain": "month"}, "y": "gifts.total_amount"},
}


def spec_lists(name: str, value: Any) -> Any:
    return lenient.spec_lists(SPEC_DIR, name, value, what="test", client="a test")


def test_single_values_become_lists_where_the_schema_wants_a_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="opsis.agent.lenient")
    spec = {
        **spec_with([{**LINE, "sort": {"field": "gifts.total_amount", "dir": "desc"}}]),
        "filters": {
            "id": "f_fund",
            "type": "multiSelect",
            "field": "gifts.fund",
            "default": "Library",
        },
    }
    fixed = spec_lists("dashboard.schema.json", spec)
    item = fixed["items"][0]
    assert item["bindings"] == {
        "x": [{"field": "gifts.gift_date", "grain": "month"}],
        "y": ["gifts.total_amount"],
    }
    assert item["sort"] == [{"field": "gifts.total_amount", "dir": "desc"}]
    # A filter control on its own, and its default (a list for multiSelect).
    assert fixed["filters"] == [
        {"id": "f_fund", "type": "multiSelect", "field": "gifts.fund", "default": ["Library"]}
    ]
    # Each one is logged, with where it was and what was sent.
    logged = [r.getMessage() for r in caplog.records]
    assert any("an object at test /items/0/bindings/x" in m for m in logged)
    assert any("a string at test /items/0/bindings/y" in m for m in logged)
    assert any("an object at test /filters" in m for m in logged)
    assert len(logged) == 5

    # Values that are already right, and values a list is not wanted for, are left alone.
    date_range = {"id": "f_date", "type": "dateRange", "field": "gifts.gift_date",
                  "default": {"relative": "last_12_months"}}  # fmt: skip
    right = {**spec_with([{**LINE, "bindings": {"x": ["gifts.fund"], "y": ["gifts.total_amount"]}}]),
             "filters": [date_range]}  # fmt: skip
    caplog.clear()
    assert spec_lists("dashboard.schema.json", right) == right
    assert caplog.records == []
    # Nothing is guessed for a value of the wrong kind: validation still reports it.
    assert spec_lists("dashboard.schema.json", {"items": None}) == {"items": None}


def test_queries_and_tool_arguments_are_forgiven_the_same_way() -> None:
    query = {
        "dataset": "gifts",
        "measures": "gifts.total_amount",
        "dimensions": "gifts.fund",
        "filters": {"field": "gifts.fund", "op": "in", "values": "Library"},
        "sort": {"field": "gifts.total_amount", "dir": "desc"},
        "timeDimension": {"field": "gifts.gift_date", "grain": "month"},
    }
    assert spec_lists("semantic-query.schema.json", query) == {
        "dataset": "gifts",
        "measures": ["gifts.total_amount"],
        "dimensions": ["gifts.fund"],
        "filters": [{"field": "gifts.fund", "op": "in", "values": ["Library"]}],
        "sort": [{"field": "gifts.total_amount", "dir": "desc"}],
        "timeDimension": {"field": "gifts.gift_date", "grain": "month"},  # an object, as wanted
    }
    # A tool's own arguments: one JSON Patch operation instead of a list of them.
    schema = {
        "type": "object",
        "properties": {"ops": {"type": "array", "items": {"type": "object"}}},
    }
    op = {"op": "replace", "path": "/title", "value": "New"}
    assert lenient.lists_of_one(schema, {"ops": op}, what="patch_dashboard") == {"ops": [op]}


def test_agents_are_forgiven_and_the_stored_spec_is_strict(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = prepared(client_for, source)
    mcp = Mcp(client)
    # create_dashboard and validate_spec take the slip.
    assert mcp.call("validate_spec", spec=spec_with([LINE]))["valid"] is True
    created = mcp.call("create_dashboard", title="Forgiven", spec=spec_with([LINE]))
    stored = db.scalar(select(DashboardVersion.spec).where(DashboardVersion.version == 1))
    assert stored is not None
    assert stored["items"][0]["bindings"]["x"] == [{"field": "gifts.gift_date", "grain": "month"}]
    # patch_dashboard: one operation instead of a list, and a binding sent on its own.
    patched = mcp.call(
        "patch_dashboard",
        id=created["id"],
        base_version=1,
        ops={"op": "replace", "path": "/items/0/bindings/y", "value": "gifts.row_count"},
    )
    assert patched["version"] == 2
    latest = mcp.call("get_dashboard", id=created["id"])["spec"]["items"][0]["bindings"]
    assert latest["y"] == ["gifts.row_count"]
    # run_query with single values.
    rows = mcp.call(
        "run_query",
        query={"dataset": "gifts", "measures": "gifts.total_amount", "dimensions": "gifts.fund",
               "filters": {"field": "gifts.fund", "op": "in", "values": "Library"}},
    )["rows"]  # fmt: skip
    assert [row[0] for row in rows] == ["Library"]

    # The REST API and the editor's JSON tab (which validates through it) stay strict.
    strict = client.post("/api/dashboards/validate", json={"spec": spec_with([LINE])}).json()
    assert strict["valid"] is False and any(
        p["path"] == "/items/0/bindings/x" for p in strict["problems"]
    )
    saved = client.put(
        f"/api/dashboards/{created['id']}", json={"spec": spec_with([LINE]), "baseVersion": 2}
    )
    assert saved.status_code == 422
    query = {"dataset": "gifts", "measures": "gifts.total_amount"}
    assert client.post("/api/query", json=query).status_code == 422
