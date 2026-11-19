"""Milestone 5: the MCP server, its tools, access tokens, JSON Patch and live events."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.dashboards import patch as patches
from opsis.db.models import (
    AccessToken,
    AuditLog,
    DashboardShare,
    DashboardVersion,
    Dataset,
    SemanticModel,
    User,
)
from opsis.errors import ApiError
from opsis.mcp import tools
from opsis.services import events, tokens, users
from tests.test_dashboards import SPEC, problems_for
from tests.test_ingestion import ok, run_jobs, settings_of
from tests.test_query_api import MODEL_YAML, load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
HEADERS = {"Accept": "application/json, text/event-stream"}


class Mcp:
    """A minimal MCP client over the real HTTP endpoint (stateless, JSON responses)."""

    def __init__(self, client: TestClient, token: str | None = None) -> None:
        self.client = client
        self.headers = dict(HEADERS)
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._id = 0

    def post(self, method: str, params: dict[str, Any] | None = None, **headers: str) -> Any:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        return self.client.post("/mcp", json=body, headers={**self.headers, **headers})

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.post(method, params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, body
        return body["result"]  # type: ignore[no-any-return]

    def call(self, name: str, /, **arguments: Any) -> Any:
        """A tool call that must succeed. Returns the parsed result."""
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"]
        assert not result.get("isError"), text
        return json.loads(text)

    def fail(self, name: str, /, **arguments: Any) -> str:
        """A tool call that must fail. Returns the error text the model would read."""
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        assert result.get("isError"), result
        return str(result["content"][0]["text"])

    def tool_names(self) -> list[str]:
        return [tool["name"] for tool in self.rpc("tools/list")["tools"]]


def prepared(client_for: ClientFor, source: dict[str, Any], **settings: Any) -> TestClient:  # noqa: F811
    client = client_for(**settings)
    load(client, source)
    return client


def make_user(db: Session, role: str, **attributes: Any) -> Principal:
    user = User(
        email=f"{role}@example.test", display_name=f"Test {role}", role=role, attributes=attributes
    )
    db.add(user)
    db.commit()
    return users.to_principal(user, "session")


def token_for(db: Session, principal: Principal, *scopes: str, days: int = 30) -> str:
    return tokens.create(db, principal, name="test", scopes=list(scopes), days=days)[1]


def spec_with(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "specVersion": 1,
        "title": "Built by a test",
        "layout": {"columns": 12, "rowHeight": 40},
        "filters": [],
        "items": items,
        "interactions": {"crossFilter": True},
    }


# ---- the catalogue --------------------------------------------------------------------


def test_tools_resources_and_prompt_are_described_for_a_model(client_for: ClientFor) -> None:
    mcp = Mcp(client_for())
    started = mcp.rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    )
    assert started["serverInfo"]["name"] == "opsis"
    assert "describe_dataset" in started["instructions"]

    listed = mcp.rpc("tools/list")["tools"]
    names = [tool["name"] for tool in listed]
    assert set(names) == {
        "list_connections", "list_datasets", "describe_dataset", "get_semantic_model",
        "list_visuals", "get_spec_schema", "preview_dataset", "run_query", "profile_column",
        "patch_semantic_model", "validate_model", "list_dashboards", "get_dashboard",
        "create_dashboard", "patch_dashboard", "validate_spec", "list_versions",
        "restore_version", "publish_dashboard", "trigger_refresh", "get_job_status",
    }  # fmt: skip
    assert names[0] == "list_datasets"  # the place to start comes first
    for tool in listed:
        description = tool["description"]
        # Every description says when to use the tool and shows one example input...
        assert "Example input: " in description, tool["name"]
        example = json.loads(description.split("Example input: ", 1)[1])
        # ...and that example is itself valid input for the tool.
        definition = tools._BY_NAME[tool["name"]]
        tools._check_arguments(definition, example)
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is (not definition.writes)
        assert len(description) < 1700, tool["name"]

    resources = {r["uri"] for r in mcp.rpc("resources/list")["resources"]}
    assert resources == {
        "opsis://guide", "opsis://visuals", "opsis://schema/dashboard",
        "opsis://schema/semantic-query", "opsis://schema/semantic-model",
        "opsis://schema/visual-manifest",
    }  # fmt: skip
    schema = mcp.rpc("resources/read", {"uri": "opsis://schema/dashboard"})["contents"][0]
    assert json.loads(schema["text"])["title"]
    visuals = mcp.rpc("resources/read", {"uri": "opsis://visuals"})["contents"][0]
    assert {v["id"] for v in json.loads(visuals["text"])} >= {"kpi", "bar", "combo", "table"}
    assert (
        "patch_dashboard"
        in mcp.rpc("resources/read", {"uri": "opsis://guide"})["contents"][0]["text"]
    )

    assert [p["name"] for p in mcp.rpc("prompts/list")["prompts"]] == ["build_dashboard"]
    prompt = mcp.rpc(
        "prompts/get",
        {"name": "build_dashboard", "arguments": {"dataset": "gifts", "goal": "giving by fund"}},
    )
    text = prompt["messages"][0]["content"]["text"]
    assert (
        '{"dataset": "gifts"}' in text and "giving by fund" in text and "create_dashboard" in text
    )

    # Stateless: no stream to listen on and no session to end.
    assert client_for().get("/mcp").status_code == 405
    assert mcp.client.delete("/mcp").status_code == 405


# ---- building a dashboard the way an agent does ---------------------------------------


def test_an_agent_can_discover_build_and_change_a_dashboard(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = prepared(client_for, source)
    mcp = Mcp(client)

    listed = {d["dataset"]: d for d in mcp.call("list_datasets")["datasets"]}
    assert listed["gifts"]["rowCount"] == 2000 and listed["gifts"]["hasData"]
    assert mcp.call("list_connections")["connections"][0]["datasetCount"] == 2

    described = mcp.call("describe_dataset", dataset="gifts")
    refs = {d["ref"]: d for d in described["dimensions"]}
    assert refs["gifts.fund"]["sampleValues"] and refs["gifts.gift_date"]["grains"]
    assert {m["ref"] for m in described["measures"]} == {"gifts.row_count", "gifts.total_amount"}
    assert described["rowCount"] == 2000 and "gifts.gift_date" in described["usage"]
    wrong = mcp.fail("describe_dataset", dataset="gift")
    assert "Did you mean 'gifts'?" in wrong and "list_datasets" in wrong

    # Every visual's example item, built from this dataset's real fields, is valid as it stands.
    visuals = mcp.call("list_visuals", dataset="gifts")["visuals"]
    assert len(visuals) == 9
    items = []
    for row, visual in enumerate(visuals):
        item = visual["exampleItem"]
        assert item is not None, visual["visual"]
        item["position"]["y"] = row * 20
        assert mcp.call("validate_spec", spec=spec_with([item])) == {
            "valid": True,
            "problems": [],
        }, visual["visual"]
        items.append(item)
    combo = next(v for v in visuals if v["visual"] == "combo")
    assert "axisChoice" in combo["roles"]["lines"] and "right" in combo["whenToUse"]
    # Without a dataset the examples use placeholders.
    placeholder = mcp.call("list_visuals")["visuals"][0]["exampleItem"]
    assert placeholder["dataset"] == "<dataset>"

    created = mcp.call(
        "create_dashboard", title="Giving", spec=spec_with(items), message="First build"
    )
    assert created["version"] == 1 and created["itemCount"] == 9
    assert created["editUrl"] == f"http://localhost:8080/dashboards/{created['id']}/edit"
    first = db.scalar(select(DashboardVersion).where(DashboardVersion.version == 1))
    assert first is not None and first.author_type == "agent" and first.message == "First build"

    # A spec sent as a JSON string, a common slip, is accepted.
    as_text = mcp.call("create_dashboard", title="From text", spec=json.dumps(spec_with([])))
    assert as_text["itemCount"] == 0
    # A title works in place of an id when it is unique.
    assert mcp.call("get_dashboard", id="giving")["id"] == created["id"]

    got = mcp.call("get_dashboard", id=created["id"])
    assert got["version"] == 1 and [i["index"] for i in got["items"]] == list(range(9))
    patched = mcp.call(
        "patch_dashboard",
        id=created["id"],
        base_version=1,
        ops=[
            {"op": "replace", "path": "/title", "value": "Giving overview"},
            {"op": "remove", "path": "/items/8"},
            {"op": "add", "path": "/items/-", "value": {
                "id": "combo_axis", "visual": "combo", "position": {"x": 0, "y": 400, "w": 6, "h": 7},
                "dataset": "gifts", "bindings": {
                    "category": ["gifts.fund"], "bars": ["gifts.total_amount"],
                    "lines": [{"field": "gifts.row_count", "axis": "right"}]}}},
        ],
        message="Rename and add a combo",
    )  # fmt: skip
    assert patched["version"] == 2 and patched["title"] == "Giving overview"

    # A stale base version is refused, and the message says how to recover.
    stale = mcp.fail(
        "patch_dashboard",
        id=created["id"],
        base_version=1,
        ops=[{"op": "remove", "path": "/items/0"}],
    )
    assert stale.startswith("Error (version_conflict)")
    assert "it is now version 2" in stale and "get_dashboard" in stale and "base_version" in stale
    current = mcp.call("get_dashboard", id=created["id"])["version"]
    recovered = mcp.call(
        "patch_dashboard",
        id=created["id"],
        base_version=current,
        ops=[{"op": "remove", "path": "/items/0"}],
    )
    assert recovered["version"] == 3 and recovered["itemCount"] == 8

    # Problems come back with their path and the names that would have worked.
    bad_field = mcp.fail(
        "patch_dashboard", id=created["id"], base_version=3,
        ops=[{"op": "add", "path": "/items/-", "value": {
            "id": "k2", "visual": "kpi", "position": {"x": 0, "y": 500, "w": 3, "h": 3},
            "dataset": "gifts", "bindings": {"value": ["gifts.total_amont"]}}}],
    )  # fmt: skip
    assert "Error (invalid_spec)" in bad_field and "/items/8/bindings/value/0" in bad_field
    assert "Did you mean 'gifts.total_amount'" in bad_field and "Nothing was saved" in bad_field
    bad_path = mcp.fail(
        "patch_dashboard",
        id=created["id"],
        base_version=3,
        ops=[{"op": "replace", "path": "/items/40/title", "value": "x"}],
    )
    assert (
        "Error (invalid_patch)" in bad_path
        and "/ops/0" in bad_path
        and "the list has 8 item(s)" in bad_path
    )
    bad_arguments = mcp.fail("patch_dashboard", id=created["id"], baseVersion=3, ops=[])
    assert (
        "Error (invalid_arguments)" in bad_arguments
        and "This tool takes: id, base_version, ops, message" in bad_arguments
    )
    assert (
        mcp.call("get_dashboard", id=created["id"])["version"] == 3
    )  # none of those saved anything
    assert "Error (unknown_tool)" in mcp.fail("drop_everything")
    assert "list_dashboards" in mcp.fail("get_dashboard", id="no such dashboard")

    history = mcp.call("list_versions", id=created["id"])
    assert [v["version"] for v in history["versions"]] == [3, 2, 1]
    assert {v["authorType"] for v in history["versions"]} == {"agent"}
    restored = mcp.call("restore_version", id=created["id"], version=1)
    assert restored["version"] == 4 and restored["itemCount"] == 9
    published = mcp.call("publish_dashboard", id=created["id"])
    assert published["publishedVersion"] == 4 and published["viewUrl"].endswith(created["id"])
    assert created["id"] in {d["id"] for d in mcp.call("list_dashboards")["dashboards"]}

    # Every write is in the audit log as an agent action, with the version it made.
    audited = db.scalars(
        select(AuditLog).where(AuditLog.action.like("agent.%")).order_by(AuditLog.id)
    ).all()
    assert [a.action for a in audited] == [
        "agent.create_dashboard", "agent.create_dashboard", "agent.patch_dashboard",
        "agent.patch_dashboard", "agent.restore_version", "agent.publish_dashboard",
    ]  # fmt: skip
    assert all(a.actor_type == "agent" and a.actor_id is not None for a in audited)
    assert audited[2].detail["tool"] == "patch_dashboard" and audited[2].detail["version"] == 2
    assert audited[2].detail["arguments"]["ops"] == [
        "replace /title",
        "remove /items/8",
        "add /items/-",
    ]
    assert audited[2].target_id == created["id"]


def test_data_model_and_refresh_tools(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = prepared(client_for, source)
    mcp = Mcp(client)

    by_fund = mcp.call("run_query", query={
        "dataset": "gifts", "measures": ["gifts.total_amount"], "dimensions": ["gifts.fund"],
        "sort": [{"field": "gifts.fund", "dir": "asc"}]})  # fmt: skip
    expected = (
        source["conn"]
        .execute("SELECT fund, sum(amount) FROM src.gifts GROUP BY 1 ORDER BY 1")
        .fetchall()
    )
    assert by_fund["rows"] == [[fund, float(total)] for fund, total in expected]
    # At most 1000 rows come back, whatever limit is asked for.
    many = mcp.call(
        "run_query", query={"dataset": "gifts", "dimensions": ["gifts.id"], "limit": 5000}
    )
    assert many["rowCount"] == 1000 and many["truncated"] and "first 1000 rows" in many["note"]
    bad = mcp.fail("run_query", query={"dataset": "gifts", "measures": ["gifts.nope"]})
    assert "What to do:" in bad

    preview = mcp.call("preview_dataset", dataset="gifts", limit=3)
    assert preview["rowsShown"] == 3 and preview["rowCount"] == 2000
    assert [c["name"] for c in preview["columns"]][:3] == ["id", "donor_id", "fund"]

    profile = mcp.call("profile_column", dataset="gifts", column="fund")
    assert profile["rows"] == 2000 and profile["nulls"] == 0 and profile["distinctValues"] == 4
    assert profile["min"] == "Library" and {v["value"] for v in profile["mostCommon"]} == {
        "Library",
        "Sport",
        "Scholarships",
        "Research",
    }
    assert sum(v["rows"] for v in profile["mostCommon"]) == 2000
    amounts = mcp.call(
        "profile_column", dataset="gifts", column="gifts.amount"
    )  # a field ref works too
    low, high = source["conn"].execute("SELECT min(amount), max(amount) FROM src.gifts").fetchone()
    assert (amounts["min"], amounts["max"]) == (float(low), float(high))
    assert "Did you mean 'fund'?" in mcp.fail("profile_column", dataset="gifts", column="fnd")
    # A column name is data to look up, never SQL.
    assert "no column called" in mcp.fail(
        "profile_column", dataset="gifts", column='fund") FROM x; --'
    )

    model = mcp.call("get_semantic_model", dataset="gifts")
    assert model["isGeneratedStarter"] and "avg_amount" not in model["definition"]["measures"]
    added = mcp.call(
        "patch_semantic_model", dataset="gifts", base_version=model["version"],
        ops=[{"op": "add", "path": "/measures/avg_amount", "value": {"agg": "avg", "column": "amount", "label": "Average gift"}}],
        message="Add average gift",
    )  # fmt: skip
    assert added["version"] == model["version"] + 1
    average = mcp.call("run_query", query={"dataset": "gifts", "measures": ["gifts.avg_amount"]})
    assert average["rows"][0][0] == pytest.approx(
        float(source["conn"].execute("SELECT avg(amount) FROM src.gifts").fetchone()[0])
    )
    conflict = mcp.fail(
        "patch_semantic_model",
        dataset="gifts",
        base_version=model["version"],
        ops=[{"op": "remove", "path": "/measures/avg_amount"}],
    )
    assert "Error (version_conflict)" in conflict and "get_semantic_model" in conflict
    invalid = mcp.fail(
        "patch_semantic_model",
        dataset="gifts",
        base_version=added["version"],
        ops=[
            {
                "op": "add",
                "path": "/measures/broken",
                "value": {"agg": "sum", "column": "no_such_column"},
            }
        ],
    )
    assert "Error (invalid_model)" in invalid and "/measures/broken" in invalid
    checked = mcp.call(
        "validate_model",
        dataset="gifts",
        definition={"measures": {"x": {"agg": "sum", "column": "nope"}}},
    )
    assert checked["valid"] is False and checked["problems"][0]["path"].startswith("/measures/x")
    assert "patch_semantic_model" in mcp.call("get_semantic_model", dataset="gifts")["note"]

    job = mcp.call("trigger_refresh", dataset="donors")
    assert job["status"] == "queued" and "get_job_status" in job["next"]
    assert "next" in mcp.call("get_job_status", job_id=job["jobId"])
    run_jobs(client)
    assert mcp.call("get_job_status", job_id=job["jobId"])["status"] == "succeeded"
    assert "jobId returned by trigger_refresh" in mcp.fail("get_job_status", job_id="soon")

    schema = mcp.call("get_spec_schema")
    assert schema["name"] == "dashboard" and schema["minimalExample"]["items"][0]["visual"] == "kpi"
    assert mcp.call("get_spec_schema", name="semantic-query")["schema"]["$defs"]["fieldRef"]


# ---- who may do what --------------------------------------------------------------------


def test_access_tokens_scopes_and_roles(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = prepared(client_for, source, mcp_auth="token")
    spec = spec_with([])

    # No token, a made-up token and a non-bearer header are all refused before MCP runs.
    for headers in (
        {},
        {"Authorization": "Bearer opsis_pat_made-up"},
        {"Authorization": "Basic abc"},
    ):
        refused = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**HEADERS, **headers},
        )
        assert refused.status_code == 401 and refused.headers["WWW-Authenticate"].startswith(
            "Bearer"
        )
        assert "Access tokens" in refused.json()["error"]["message"]

    # Tokens are created through the REST API, shown once, and stored only as a hash.
    made = ok(
        client.post("/api/tokens", json={"name": "Laptop", "scopes": ["read", "edit"], "days": 7}),
        201,
    )
    secret = made["token"]
    assert (
        secret.startswith("opsis_pat_")
        and made["prefix"] == secret[:16]
        and made["status"] == "active"
    )
    listing = ok(client.get("/api/tokens"))
    assert listing["mcpUrl"] == "http://localhost:8080/mcp" and listing["mcpAuth"] == "token"
    assert "token" not in listing["tokens"][0] and listing["tokens"][0]["lastUsedAt"] is None
    stored = db.scalar(select(AccessToken))
    assert stored is not None and secret not in json.dumps(
        [stored.token_hash, stored.prefix, stored.name]
    )
    assert len(stored.token_hash) == 64
    for body in (
        {"name": "x", "scopes": ["root"]},
        {"name": "x", "scopes": []},
        {"name": "x", "scopes": ["read"], "days": 5000},
    ):
        assert client.post("/api/tokens", json=body).status_code == 422

    admin = Mcp(client, secret)
    assert "publish_dashboard" not in admin.tool_names() and "patch_dashboard" in admin.tool_names()
    created = admin.call("create_dashboard", title="Scoped", spec=spec)
    no_scope = admin.fail("publish_dashboard", id=created["id"])
    assert "does not have the `publish` scope" in no_scope and "Do not retry" in no_scope
    db.expire_all()
    assert db.scalar(select(AccessToken.last_used_at)) is not None

    # A read-only token sees no tools that write, and cannot call them by name either.
    admin_principal = users.to_principal(users.get_builtin_admin(db), "none")  # type: ignore[arg-type]
    reader = Mcp(client, token_for(db, admin_principal, "read"))
    assert (
        set(reader.tool_names())
        & {
            "create_dashboard",
            "patch_dashboard",
            "restore_version",
            "trigger_refresh",
            "patch_semantic_model",
            "publish_dashboard",
        }
        == set()
    )
    assert "describe_dataset" in reader.tool_names()
    assert "`edit` scope" in reader.fail("create_dashboard", title="No", spec=spec)

    # A token never gives more than its owner's role allows.
    viewer = make_user(db, "viewer")
    as_viewer = Mcp(client, token_for(db, viewer, "read", "edit", "publish", "admin"))
    assert as_viewer.tool_names() == ["list_dashboards", "get_dashboard"]
    assert "does not allow create_dashboard" in as_viewer.fail(
        "create_dashboard", title="No", spec=spec
    )
    assert as_viewer.call("list_dashboards")["dashboards"] == []  # nothing is published yet
    # Not shared with them: as if it did not exist.
    assert "no dashboard with the id or title" in as_viewer.fail("get_dashboard", id=created["id"])
    db.add(
        DashboardShare(
            dashboard_id=uuid.UUID(created["id"]), grantee=f"user:{viewer.user_id}", role="viewer"
        )
    )
    db.commit()
    assert "no dashboard with the id or title" in as_viewer.fail(
        "get_dashboard", id=created["id"]
    )  # not published
    everything = Mcp(client, token_for(db, admin_principal, "read", "edit", "publish"))
    everything.call("publish_dashboard", id=created["id"])
    assert as_viewer.call("get_dashboard", id=created["id"])["version"] == 1

    # Calls run as the token's owner: two tokens, two identities, in turn.
    editor = make_user(db, "editor", regions=["VIC"])
    as_editor = Mcp(client, token_for(db, editor, "read", "edit"))
    theirs = as_editor.call("create_dashboard", title="Editor's", spec=spec)
    mine = everything.call("create_dashboard", title="Admin's", spec=spec)
    authors = {str(v.dashboard_id): v.author_id for v in db.scalars(select(DashboardVersion)).all()}
    assert (
        authors[theirs["id"]] == editor.user_id and authors[mine["id"]] == admin_principal.user_id
    )

    # Revoked, expired and deactivated all stop working at once.
    ok(client.post(f"/api/tokens/{made['id']}/revoke"))
    assert admin.post("tools/list").status_code == 401
    assert ok(client.get("/api/tokens"))["tokens"][-1]["status"] == "revoked"
    expiring = token_for(db, editor, "read")
    db.execute(
        AccessToken.__table__.update()
        .where(AccessToken.token_hash == tokens._hash(expiring))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    db.commit()
    assert Mcp(client, expiring).post("tools/list").status_code == 401
    db.execute(User.__table__.update().where(User.id == editor.user_id).values(is_active=False))
    db.commit()
    assert as_editor.post("tools/list").status_code == 401

    # Someone else's token cannot be revoked or deleted.
    other = db.scalar(select(AccessToken).where(AccessToken.user_id == viewer.user_id))
    assert other is not None
    assert client.post(f"/api/tokens/{other.id}/revoke").status_code == 404
    assert client.delete(f"/api/tokens/{other.id}").status_code == 404
    assert client.delete(f"/api/tokens/{made['id']}").status_code == 204

    # A page on another site cannot call the endpoint from a browser.
    everything_headers = {"Authorization": everything.headers["Authorization"]}
    assert everything.post("tools/list", Origin="https://evil.example").status_code == 403
    assert everything.post("tools/list", Origin="http://localhost:8080").status_code == 200
    assert everything.post("tools/list", Origin="http://127.0.0.1:6274").status_code == 200
    assert everything_headers  # (kept for readability of the three lines above)


def test_row_level_security_holds_through_the_tools(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    client = prepared(client_for, source, mcp_auth="token")
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))  # rule: region IN user.regions
    editor = make_user(db, "editor", regions=["VIC"])
    mcp = Mcp(client, token_for(db, editor, "read", "edit"))

    rows = mcp.call(
        "run_query",
        query={
            "dataset": "gifts",
            "measures": ["gifts.gift_count"],
            "dimensions": ["gifts.region"],
        },
    )["rows"]
    vic = (
        source["conn"].execute("SELECT count(*) FROM src.gifts WHERE region = 'VIC'").fetchone()[0]
    )
    assert rows == [["VIC", vic]]

    # The tools that read rows directly go through the same filter (decided at the M5
    # review), and do not reveal how many rows are hidden.
    preview = mcp.call("preview_dataset", dataset="gifts", limit=100)
    region = [c["name"] for c in preview["columns"]].index("region")
    assert preview["rowsShown"] == 100 and {row[region] for row in preview["rows"]} == {"VIC"}
    assert preview["rowCount"] is None and "Row-level security applies" in preview["note"]
    profile = mcp.call("profile_column", dataset="gifts", column="region")
    assert profile["rows"] == vic and profile["distinctValues"] == 1
    assert (
        profile["mostCommon"] == [{"value": "VIC", "rows": vic}]
        and "only your rows" in profile["note"]
    )
    described = mcp.call("describe_dataset", dataset="gifts")
    samples = {d["ref"]: d.get("sampleValues") for d in described["dimensions"]}
    assert samples["gifts.region"] == ["VIC"] and described["rowCount"] is None
    assert "row-level security" in described["sampleValuesNote"].lower()
    # A dataset without rules is unaffected.
    donors = mcp.call("preview_dataset", dataset="donors", limit=2)
    assert donors["rowsShown"] == 2 and donors["rowCount"] == 40

    # A rule that cannot be applied (here: its column has gone) refuses; it never shows
    # raw rows instead.
    record = db.scalar(
        select(SemanticModel)
        .join(Dataset, Dataset.id == SemanticModel.dataset_id)
        .where(Dataset.name == "gifts")
    )
    assert record is not None and record.definition["rls"]
    record.definition = {
        **record.definition,
        "rls": [{"filter": "vanished_column IN {{ user.attributes.regions }}"}],
    }
    db.commit()
    for tool, arguments in (("preview_dataset", {}), ("profile_column", {"column": "fund"})):
        refused = mcp.fail(tool, dataset="gifts", **arguments)
        assert "Error (invalid_model)" in refused and "row-level security rule" in refused

    # An admin is exempt from the rule, as in the rest of the app.
    admin = users.to_principal(users.get_builtin_admin(db), "none")  # type: ignore[arg-type]
    assert (
        Mcp(client, token_for(db, admin, "read")).call("preview_dataset", dataset="gifts", limit=2)[
            "rowsShown"
        ]
        == 2
    )


# ---- JSON Patch -------------------------------------------------------------------------


def test_json_patch_operations_and_messages() -> None:
    document = {
        "title": "A",
        "items": [{"id": "a", "n": 1}, {"id": "b"}],
        "meta": {"k/ey": 1, "t~x": 2},
    }
    original = copy.deepcopy(document)
    result = patches.apply(document, [
        {"op": "replace", "path": "/title", "value": "B"},
        {"op": "add", "path": "/items/-", "value": {"id": "c"}},
        {"op": "add", "path": "/items/0", "value": {"id": "first"}},
        {"op": "remove", "path": "/items/1/n"},
        {"op": "copy", "from": "/items/0", "path": "/copy"},
        {"op": "move", "from": "/items/3", "path": "/items/0"},
        {"op": "test", "path": "/meta/k~1ey", "value": 1},
        {"op": "replace", "path": "/meta/t~0x", "value": 3},
        {"op": "add", "path": "/meta/new", "value": [1]},
    ])  # fmt: skip
    assert result == {
        "title": "B",
        "items": [{"id": "c"}, {"id": "first"}, {"id": "a"}, {"id": "b"}],
        "meta": {"k/ey": 1, "t~x": 3, "new": [1]},
        "copy": {"id": "first"},
    }
    assert document == original  # the input is never changed

    def message(ops: Any) -> str:
        with pytest.raises(ApiError) as caught:
            patches.apply(document, ops)
        assert caught.value.code == "invalid_patch" and caught.value.status == 422
        assert document == original
        return f"{caught.value.details[0]['path']} {caught.value.message}"

    assert "must be a non-empty list" in message([])
    assert message("nope").startswith("/ops ")
    assert "/ops/1 Operation 1 could not be applied, so nothing was changed" in message(
        [{"op": "replace", "path": "/title", "value": "x"}, {"op": "remove", "path": "/nope"}]
    )
    assert "there is nothing at /items/0/zzz" in message(
        [{"op": "replace", "path": "/items/0/zzz/deep", "value": 1}]
    )
    assert "an object with the keys: id, n" in message(
        [{"op": "replace", "path": "/items/0/zzz/deep", "value": 1}]
    )
    assert "the list has 2 item(s), so the last position is 1" in message(
        [{"op": "remove", "path": "/items/2"}]
    )
    assert "Use the `add` operation to create it" in message(
        [{"op": "replace", "path": "/description", "value": "x"}]
    )
    assert "is not a list position" in message(
        [{"op": "replace", "path": "/items/first", "value": 1}]
    )
    assert 'must start with "/"' in message([{"op": "add", "path": "items/-", "value": 1}])
    assert "only works with `add`" in message([{"op": "remove", "path": "/items/-"}])
    assert "must be one of: add, remove, replace, move, copy, test" in message(
        [{"op": "set", "path": "/title"}]
    )
    assert "needs a `value`" in message([{"op": "add", "path": "/title"}])
    assert "needs a `from` path" in message([{"op": "move", "path": "/title"}])
    assert "the `test` failed" in message([{"op": "test", "path": "/title", "value": "Z"}])
    assert "into itself" in message([{"op": "move", "from": "/meta", "path": "/meta/inner"}])
    assert "Too many operations" in message([{"op": "test", "path": "/title", "value": "A"}] * 201)
    assert "Each operation must be an object" in message(["remove /title"])


# ---- the combo chart's second axis --------------------------------------------------------


def test_axis_choice_is_only_for_roles_that_offer_it(
    client_for: ClientFor,
    source: dict[str, Any],  # noqa: F811
) -> None:
    client = prepared(client_for, source)
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))

    def spec(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **SPEC,
            "filters": [],
            "items": [
                {
                    "id": "v",
                    "position": {"x": 0, "y": 0, "w": 6, "h": 6},
                    "dataset": "gifts",
                    **item,
                }
            ],
        }

    combo = {"visual": "combo", "bindings": {
        "category": ["gifts.fund"], "bars": [{"field": "gifts.total_amount", "axis": "left"}],
        "lines": [{"field": "gifts.gift_count", "axis": "right"}]}}  # fmt: skip
    assert problems_for(client, spec(combo)) == {}
    bar = {
        "visual": "bar",
        "bindings": {
            "category": ["gifts.fund"],
            "value": [{"field": "gifts.gift_count", "axis": "right"}],
        },
    }
    found = problems_for(client, spec(bar))
    assert (
        list(found) == ["/items/0/bindings/value/0/axis"]
        and "Remove `axis`" in found["/items/0/bindings/value/0/axis"]
    )
    sideways = {
        "visual": "combo",
        "bindings": {
            "category": ["gifts.fund"],
            "bars": ["gifts.total_amount"],
            "lines": [{"field": "gifts.gift_count", "axis": "top"}],
        },
    }
    assert any(
        path.startswith("/items/0/bindings/lines/0")
        for path in problems_for(client, spec(sideways))
    )
    # The axis choice does not change the data that is fetched.
    dashboard = ok(client.post("/api/dashboards", json={"title": "x", "spec": spec(combo)}), 201)[
        "id"
    ]
    body = ok(client.post(f"/api/dashboards/{dashboard}/data", json={"itemId": "v"}))
    assert [c["name"] for c in body["columns"]] == [
        "gifts.fund",
        "gifts.total_amount",
        "gifts.gift_count",
    ]


# ---- live events ----------------------------------------------------------------------------


def test_event_hub_delivers_from_other_threads() -> None:
    hub = events.EventHub()

    async def scenario() -> list[events.Event]:
        received: list[events.Event] = []

        async def listen() -> None:
            async for event in hub.subscribe("dashboard:1"):
                received.append(event)
                if len(received) == 2:
                    return

        listener = asyncio.create_task(listen())
        while hub.count("dashboard:1") == 0:
            await asyncio.sleep(0.01)
        hub.publish("dashboard:other", "version_created", {"version": 99})  # another channel
        thread = threading.Thread(
            target=lambda: [
                hub.publish("dashboard:1", "version_created", {"version": n}) for n in (2, 3)
            ]
        )
        thread.start()
        await asyncio.wait_for(listener, timeout=5)
        thread.join()
        return received

    assert asyncio.run(scenario()) == [
        ("version_created", {"version": 2}),
        ("version_created", {"version": 3}),
    ]
    assert hub.count("dashboard:1") == 0  # the listener was removed when it stopped
    hub.publish("dashboard:1", "version_created", {"version": 4})  # nobody listening: no error


def test_writes_announce_themselves_to_open_editors(
    client_for: ClientFor,
    source: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    client = prepared(client_for, source)
    seen: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        events.hub, "publish", lambda channel, event, data: seen.append((channel, event, data))
    )
    mcp = Mcp(client)

    created = mcp.call("create_dashboard", title="Live", spec=spec_with([]))
    channel = f"dashboard:{created['id']}"
    mcp.call(
        "patch_dashboard",
        id=created["id"],
        base_version=1,
        ops=[{"op": "replace", "path": "/title", "value": "Live 2"}],
        message="Rename",
    )
    saved = ok(
        client.put(
            f"/api/dashboards/{created['id']}",
            json={"spec": spec_with([]), "baseVersion": 2},
            headers={"X-Opsis-Client": "tab-1"},
        )
    )
    assert saved["version"] == 3
    ok(client.post(f"/api/dashboards/{created['id']}/publish", headers={"X-Opsis-Client": "tab-1"}))

    assert [(c, e) for c, e, _ in seen] == [
        (channel, "version_created"),
        (channel, "version_created"),
        (channel, "published"),
    ]
    by_agent, by_person, published = (data for _, _, data in seen)
    assert by_agent == {
        "dashboardId": created["id"], "version": 2, "publishedVersion": None, "authorType": "agent",
        "authorName": "Local admin", "message": "Rename", "clientId": None,
    }  # fmt: skip
    assert (by_person["authorType"], by_person["clientId"], by_person["version"]) == (
        "user",
        "tab-1",
        3,
    )
    assert published["publishedVersion"] == 3
    # A refused write announces nothing.
    assert "version_conflict" in mcp.fail(
        "patch_dashboard",
        id=created["id"],
        base_version=1,
        ops=[{"op": "remove", "path": "/title"}],
    )
    assert len(seen) == 3
    assert settings_of(client).sse_keepalive_seconds > 0
