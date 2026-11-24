"""The demo data (M8): three made-up datasets, their models and a dashboard."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsis.db.models import Dashboard, Dataset
from opsis.services import demo
from tests.test_ingestion import ok, run_jobs

ClientFor = Callable[..., TestClient]


def test_demo_data(client_for: ClientFor, db: Session) -> None:
    client = client_for()
    assert ok(client.get("/api/admin/demo")) == {"loaded": False, "dashboardId": None}
    job = ok(client.post("/api/admin/demo"), 202)
    run_jobs(client)
    done = ok(client.get(f"/api/jobs/{job['id']}"))
    assert done["status"] == "succeeded", done.get("error")
    status = ok(client.get("/api/admin/demo"))
    assert status["loaded"] is True

    listed = {d["name"]: d for d in ok(client.get("/api/datasets"))}
    assert {"demo_gifts", "demo_donors", "demo_regions"} <= set(listed)
    gifts = ok(client.get("/api/models/demo_gifts"))
    assert gifts["isStarter"] is False and "demo_regions.state" in {
        f["ref"] for f in gifts["fields"]
    }

    # Published, and every visual has rows, the state table through two joins.
    dashboard_id = status["dashboardId"]
    seen = ok(client.get(f"/api/dashboards/{dashboard_id}"))
    assert seen["publishedVersion"] is not None and seen["spec"]["title"] == demo.TITLE
    for item in seen["spec"]["items"]:
        data = ok(
            client.post(
                f"/api/dashboards/{dashboard_id}/data",
                json={"itemId": item["id"], "source": "published"},
            )
        )
        assert data["rows"], item["id"]
    states = ok(
        client.post(
            f"/api/dashboards/{dashboard_id}/data",
            json={"itemId": "by_state", "source": "published"},
        )
    )
    assert {row[0] for row in states["rows"]} <= {name for _, name, _ in demo.STATES}
    total = ok(
        client.post("/api/query", json={"query": {"dataset": "demo_gifts", "measures": [
            "demo_gifts.gifts", "demo_gifts.donors"]}})
    )["rows"][0]  # fmt: skip
    assert total[0] == demo.GIFT_ROWS and 0 < total[1] <= demo.DONOR_ROWS

    # Again: refused, and nothing is made twice.
    again = ok(client.post("/api/admin/demo"), 202)
    run_jobs(client)
    refused = ok(client.get(f"/api/jobs/{again['id']}"))
    assert refused["status"] == "failed" and "already loaded" in refused["error"]
    assert db.scalar(select(func.count()).where(Dashboard.title == demo.TITLE)) == 1
    names = ("demo_gifts", "demo_donors", "demo_regions")
    assert db.scalar(select(func.count()).where(Dataset.name.in_(names))) == 3


def test_the_demo_rows_are_the_same_for_the_same_day() -> None:
    first = demo.generate(date(2026, 11, 24))
    assert first == demo.generate(date(2026, 11, 24))
    assert first != demo.generate(date(2026, 11, 25))
    gifts = first["demo_gifts"].decode().splitlines()
    assert gifts[0] == "gift_id,donor_id,gift_date,fund,channel,amount"
    assert len(gifts) == demo.GIFT_ROWS + 1
    dates = sorted(line.split(",")[2] for line in gifts[1:])
    assert dates[0] >= "2023-11-25" and dates[-1] <= "2026-11-24"


def test_demo_data_in_server_mode(client_for: ClientFor) -> None:
    """The way the server runs it: signed in, at /opsis, from Settings."""
    from tests.test_auth import SERVER_SIGN_IN, admin_client

    admin = admin_client(client_for, **SERVER_SIGN_IN)
    job = ok(admin.post("/api/admin/demo"), 202)
    run_jobs(admin)
    done = ok(admin.get(f"/api/jobs/{job['id']}"))
    assert done["status"] == "succeeded", done.get("error")
    status = ok(admin.get("/api/admin/demo"))
    assert status["loaded"] is True
    assert done["result"]["url"] == f"https://bi.test/opsis/dashboards/{status['dashboardId']}"
