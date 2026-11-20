"""The M6 review: dataset owners restrict their datasets, only owners and admins
change sharing, and restricted data cannot leave its list through a dashboard
unless the dataset's owner or an admin opens it up."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_auth import SERVER_SIGN_IN, admin_client, person
from tests.test_ingestion import connect, make_dataset, ok, run_jobs
from tests.test_query_api import MODEL_YAML
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]


def kpi(item_id: str, dataset: str, measure: str, y: int = 0) -> dict[str, Any]:
    return {"id": item_id, "visual": "kpi", "position": {"x": 0, "y": y, "w": 3, "h": 3},
            "dataset": dataset, "bindings": {"value": [f"{dataset}.{measure}"]}}  # fmt: skip


def spec(title: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {
        "specVersion": 1,
        "title": title,
        "layout": {"columns": 12, "rowHeight": 40},
        "filters": [],
        "items": list(items),
        "interactions": {"crossFilter": True},
    }


GIFTS = kpi("g", "gifts", "total_amount")
DONORS = kpi("d", "donors", "row_count", y=3)


def test_restricted_data_and_sharing_rights(
    client_for: ClientFor,
    source: dict[str, Any],
    db: Session,  # noqa: F811
) -> None:
    admin = admin_client(client_for, **SERVER_SIGN_IN)
    # Ann creates the donors dataset, so she owns it; the admin creates gifts.
    ann, ann_id = person(client_for, admin, "ann@example.test", "editor", **SERVER_SIGN_IN)
    ben, ben_id = person(client_for, admin, "ben@example.test", "editor", **SERVER_SIGN_IN)
    cat, cat_id = person(client_for, admin, "cat@example.test", "editor", **SERVER_SIGN_IN)
    vic, vic_id = person(client_for, admin, "vic@example.test", "viewer", **SERVER_SIGN_IN)
    connection = connect(admin, "postgres", source["config"])
    gifts = make_dataset(admin, connection, "src.gifts", name="gifts")["id"]
    donors = make_dataset(ann, connection, "src.donors", name="donors")["id"]
    run_jobs(admin)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    assert ok(ann.get(f"/api/datasets/{donors}"))["createdBy"] == ann_id
    assert gifts

    # ---- 1. The owner restricts her dataset; an admin's restriction is the admins' alone.
    finance = ok(admin.post("/api/groups", json={"name": "Finance", "memberIds": [ben_id]}), 201)
    in_finance = [f"group:{finance['id']}"]
    assert ben.put(f"/api/datasets/{donors}/access", json={"restricted": True}).status_code == 403
    mine = ok(
        ann.put(f"/api/datasets/{donors}/access", json={"restricted": True, "grants": in_finance})
    )
    assert (mine["restricted"], mine["restrictedByAdmin"], mine["canManage"]) == (True, False, True)
    assert mine["owner"]["id"] == ann_id and mine["restrictedBy"] == "ann"  # her display name
    assert (
        ok(ann.put(f"/api/datasets/{donors}/access", json={"restricted": False}))["restricted"]
        is False
    )
    ok(admin.put(f"/api/datasets/{donors}/access", json={"restricted": True, "grants": in_finance}))
    # Decided at the M7 review: once an admin restricts it, the owner can neither lift
    # the restriction nor change its list, and sees it read-only.
    with_cat = [*in_finance, f"user:{cat_id}"]
    for attempt in (
        {"restricted": False, "grants": in_finance},
        {"restricted": True, "grants": with_cat},
    ):
        refused = ann.put(f"/api/datasets/{donors}/access", json=attempt)
        assert refused.status_code == 403
        assert "only an admin can change who is on its list" in refused.json()["error"]["message"]
    seen = ok(ann.get(f"/api/datasets/{donors}/access"))
    assert (seen["restrictedByAdmin"], seen["canManage"]) == (True, False)
    assert [g["grantee"] for g in seen["grants"]] == in_finance  # unchanged
    assert ok(admin.get(f"/api/datasets/{donors}/access"))["canManage"] is True
    # The owner can use her dataset even though she is not on its list; others cannot.
    query = {"query": {"dataset": "donors", "measures": ["donors.row_count"]}}
    assert ok(ann.post("/api/query", json=query))["rows"] == [[40]]
    assert ok(ben.post("/api/query", json=query))["rows"] == [[40]]  # through Finance
    assert cat.post("/api/query", json=query).status_code == 403

    # ---- 2. Only the owner and admins change sharing; editors change content.
    shared = ok(
        ben.post("/api/dashboards", json={"title": "Ben's", "spec": spec("Ben's", GIFTS)}), 201
    )["id"]
    ok(
        ben.put(
            f"/api/dashboards/{shared}/access",
            json={"shares": [{"grantee": f"user:{cat_id}", "role": "editor"}]},
        )
    )
    assert (
        ok(cat.put(f"/api/dashboards/{shared}", json={"spec": spec("Cat edited", GIFTS)}))[
            "version"
        ]
        == 2
    )
    detail = ok(cat.get(f"/api/dashboards/{shared}"))
    assert detail["canEdit"] is True and detail["canShare"] is False
    for attempt in (
        cat.get(f"/api/dashboards/{shared}/access"),
        cat.put(f"/api/dashboards/{shared}/access", json={"shares": []}),
        cat.post(f"/api/dashboards/{shared}/links", json={}),
    ):
        assert (
            attempt.status_code == 403 and "owner or an admin" in attempt.json()["error"]["message"]
        )

    # ---- 3. Restricted data through dashboards.
    both = spec("Board", GIFTS, DONORS)
    board = ok(ben.post("/api/dashboards", json={"title": "Board", "spec": both}), 201)["id"]
    ok(ben.post(f"/api/dashboards/{board}/publish"))
    # Ben may use donors, but he is not its owner: he cannot open it up.
    refused = ben.put(
        f"/api/dashboards/{board}/access",
        json={"shares": [{"grantee": f"user:{vic_id}", "role": "viewer"}]},
    )
    message = refused.json()["error"]["message"]
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "restricted_data"
    assert "'donors' (owner: ann)" in message and "vic@example.test" in message
    assert "Only a dataset's owner or an admin" in message
    everyone = ben.put(
        f"/api/dashboards/{board}/access",
        json={"shares": [{"grantee": "everyone", "role": "viewer"}]},
    )
    assert everyone.status_code == 403 and "Everyone" in everyone.json()["error"]["message"]
    inside = ok(
        ben.put(
            f"/api/dashboards/{board}/access",
            json={"shares": [{"grantee": f"group:{finance['id']}", "role": "viewer"}]},
        )
    )
    context = inside["restricted"]
    assert [c["dataset"] for c in context] == ["donors"] and context[0]["canOpenUp"] is False
    assert {ben_id, ann_id} <= set(context[0]["insideUserIds"]) and context[0][
        "insideGroupIds"
    ] == [finance["id"]]
    link = ben.post(f"/api/dashboards/{board}/links", json={})
    assert (
        link.status_code == 403 and "create a share link for it" in link.json()["error"]["message"]
    )

    # The agent follows the same rules.
    token = ok(
        ben.post("/api/tokens", json={"name": "agent", "scopes": ["read", "edit", "publish"]}), 201
    )["token"]
    mcp = client_for(**SERVER_SIGN_IN, csrf=False)
    called = mcp.post("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                          "name": "share_dashboard", "arguments": {"id": board, "with": "vic@example.test"}}})  # fmt: skip
    result = called.json()["result"]
    assert result["isError"] and "Error (restricted_data)" in result["content"][0]["text"]
    for name, arguments in (("create_share_link", {"id": board}),):
        text = mcp.post("/mcp", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}}).json()["result"]["content"][0]["text"]  # fmt: skip
        assert "restricted_data" in text

    # The dataset's owner (as dashboard owner) and admins may open it up, and the
    # dataset's page then lists where its data is visible outside the list.
    ok(
        admin.put(
            f"/api/dashboards/{board}/access",
            json={"shares": [{"grantee": f"user:{vic_id}", "role": "viewer"}]},
        )
    )
    assert ok(
        vic.post(f"/api/dashboards/{board}/data", json={"itemId": "d", "source": "published"})
    )["rows"] == [[40]]
    owned = ok(
        ann.post("/api/dashboards", json={"title": "Ann's", "spec": spec("Ann's", DONORS)}), 201
    )["id"]
    ok(ann.post(f"/api/dashboards/{owned}/publish"))
    ok(
        ann.put(
            f"/api/dashboards/{owned}/access",
            json={"shares": [{"grantee": "everyone", "role": "viewer"}]},
        )
    )
    assert ok(ann.post(f"/api/dashboards/{owned}/links", json={}), 201)["status"] == "active"
    exposed = {e["title"]: e for e in ok(ann.get(f"/api/datasets/{donors}/access"))["exposures"]}
    assert exposed["Board"]["outside"] == ["vic@example.test"]
    assert exposed["Ann's"]["outside"] == ["Everyone"] and exposed["Ann's"]["shareLinks"] == 1

    # A draft cannot pull restricted data in front of editors outside its list...
    review = ok(
        ben.post("/api/dashboards", json={"title": "Review", "spec": spec("Review", GIFTS)}), 201
    )["id"]
    ok(
        ben.put(
            f"/api/dashboards/{review}/access",
            json={"shares": [{"grantee": f"user:{cat_id}", "role": "editor"}]},
        )
    )
    pulled = ben.put(f"/api/dashboards/{review}", json={"spec": spec("Review", GIFTS, DONORS)})
    assert (
        pulled.status_code == 403
        and "add it to a dashboard that people outside its list edit"
        in pulled.json()["error"]["message"]
    )
    # ...and publishing cannot show it to viewers outside its list.
    report = ok(
        ben.post("/api/dashboards", json={"title": "Report", "spec": spec("Report", GIFTS)}), 201
    )["id"]
    ok(ben.post(f"/api/dashboards/{report}/publish"))
    ok(
        ben.put(
            f"/api/dashboards/{report}/access",
            json={"shares": [{"grantee": f"user:{vic_id}", "role": "viewer"}]},
        )
    )
    ok(
        ben.put(f"/api/dashboards/{report}", json={"spec": spec("Report", GIFTS, DONORS)})
    )  # only Ben edits
    published = ben.post(f"/api/dashboards/{report}/publish")
    assert (
        published.status_code == 403
        and "publish it to people outside its list" in published.json()["error"]["message"]
    )
    assert ok(admin.post(f"/api/dashboards/{report}/publish"))["publishedVersion"] == 2

    # ---- 4. A dashboard answers its own questions: cross-filters on other fields are ignored.
    total = ok(ben.post(f"/api/dashboards/{shared}/data", json={"itemId": "g"}))["rows"]
    probing = {"cross": [{"field": "donors.tier", "values": ["major"], "source": "x"}]}
    assert (
        ok(ben.post(f"/api/dashboards/{shared}/data", json={"itemId": "g", "state": probing}))[
            "rows"
        ]
        == total
    )
    assert json.dumps(total)
