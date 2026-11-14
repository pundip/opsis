"""Milestone 3 through the API: models, YAML, and queries checked against hand-written SQL."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from opsis.db.models import AuditLog, SemanticModel
from opsis.services import models
from opsis.settings import Settings
from tests.test_ingestion import connect, make_dataset, ok, refresh, run_jobs

ClientFor = Callable[..., TestClient]

MODEL_YAML = """
dataset: gifts
label: Gifts
dimensions:
  fund: { column: fund, type: string }
  region: { column: region, type: string }
  gift_date: { column: gift_date, type: time }
  band: { expr: "CASE WHEN amount >= 500 THEN 'large' ELSE 'small' END", type: string }
measures:
  total_amount: { agg: sum, column: amount, format: currency_aud }
  gift_count: { agg: count }
  donors: { agg: count_distinct, column: donor_id }
  avg_gift: { expr: "{total_amount} / NULLIF({gift_count}, 0)", format: currency_aud }
segments:
  large: { filter: "amount >= 500" }
joins:
  - to: donors
    type: left
    on: { donor_id: id }
    relationship: many_to_one
rls:
  - filter: "region IN {{ user.attributes.regions }}"
    applies_to_roles: [viewer, editor]
"""


@pytest.fixture
def source(migrated_db: Settings) -> Iterator[dict[str, Any]]:
    """gifts and donors tables in the test Postgres, with deterministic rows."""
    config = {
        "host": migrated_db.postgres_host,
        "port": migrated_db.postgres_port,
        "database": migrated_db.postgres_db,
        "user": migrated_db.postgres_user,
        "password": migrated_db.postgres_password.get_secret_value(),
    }
    conninfo = {k if k != "database" else "dbname": v for k, v in config.items()}
    with psycopg.connect(**conninfo, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS src CASCADE")
        conn.execute("CREATE SCHEMA src")
        conn.execute(
            "CREATE TABLE src.donors (id integer PRIMARY KEY, name text, tier text, region text)"
        )
        conn.execute(
            """INSERT INTO src.donors
               SELECT n, 'Donor ' || n, CASE WHEN n % 4 = 0 THEN 'major' ELSE 'regular' END,
                      (ARRAY['VIC','NSW','QLD'])[1 + n % 3]
               FROM generate_series(1, 40) n"""
        )
        conn.execute(
            """CREATE TABLE src.gifts (id integer PRIMARY KEY, donor_id integer, fund text,
                 amount numeric(12,2), gift_date date, region text)"""
        )
        conn.execute(
            """INSERT INTO src.gifts
               SELECT n, 1 + n % 45, (ARRAY['Library','Sport','Scholarships','Research'])[1 + n % 4],
                      round((10 + (n * 37) % 990 + (n % 100) / 100.0)::numeric, 2),
                      DATE '2025-01-01' + ((n * 3) % 720), (ARRAY['VIC','NSW','QLD'])[1 + n % 3]
               FROM generate_series(1, 2000) n"""
        )
        yield {"config": config, "conn": conn}
        conn.execute("DROP SCHEMA IF EXISTS src CASCADE")


def load(client: TestClient, source: dict[str, Any]) -> dict[str, str]:
    connection = connect(client, "postgres", source["config"])
    ids = {}
    for table in ("gifts", "donors"):
        ids[table] = make_dataset(client, connection, f"src.{table}", name=table)["id"]
    run_jobs(client)
    return ids


def query(
    client: TestClient, body: dict[str, Any], status: int = 200, **extra: Any
) -> dict[str, Any]:
    return ok(client.post("/api/query", json={"query": body, **extra}), status)  # type: ignore[no-any-return]


def as_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    names = [c["name"] for c in result["columns"]]
    return [dict(zip(names, row, strict=True)) for row in result["rows"]]


def test_starter_models_are_created_with_the_first_refresh(
    client_for: ClientFor, source: dict[str, Any]
) -> None:
    client = client_for()
    load(client, source)
    listed = {m["dataset"]: m for m in ok(client.get("/api/models"))}
    assert listed["gifts"]["saved"] and listed["gifts"]["isStarter"]
    assert listed["gifts"]["dimensions"] == 6 and listed["gifts"]["measures"] == 2

    model = ok(client.get("/api/models/gifts"))
    assert model["definition"]["measures"] == {
        "row_count": {"agg": "count", "label": "Rows"},
        "total_amount": {"agg": "sum", "column": "amount", "label": "Total amount"},
    }
    assert model["definition"]["dimensions"]["gift_date"] == {"column": "gift_date", "type": "time"}
    assert {"ref": "gifts.total_amount", "kind": "measure"}.items() <= next(
        f for f in model["fields"] if f["ref"] == "gifts.total_amount"
    ).items()
    assert models.from_yaml(model["yaml"])["dataset"] == "gifts"

    # A starter is usable straight away.
    result = query(
        client, {"dataset": "gifts", "measures": ["gifts.row_count", "gifts.total_amount"]}
    )
    expected = source["conn"].execute("SELECT count(*), sum(amount) FROM src.gifts").fetchone()
    assert result["rows"] == [[expected[0], float(expected[1])]]

    # An unedited starter follows the source's columns; an edited model is left alone.
    ok(
        client.put(
            "/api/models/donors",
            json={"definition": {"dimensions": {"tier": {"column": "tier", "type": "string"}}}},
        )
    )
    source["conn"].execute("ALTER TABLE src.gifts ADD COLUMN channel text")
    source["conn"].execute("ALTER TABLE src.donors ADD COLUMN email text")
    for name in ("gifts", "donors"):
        dataset = next(d for d in ok(client.get("/api/datasets")) if d["name"] == name)
        assert refresh(client, dataset["id"], full=True)["lastJob"]["status"] == "succeeded"
    assert "channel" in ok(client.get("/api/models/gifts"))["definition"]["dimensions"]
    donors = ok(client.get("/api/models/donors"))
    assert list(donors["definition"]["dimensions"]) == ["tier"] and not donors["isStarter"]
    # The starter endpoint shows what a regenerated model would be, without saving it.
    assert "email" in ok(client.get("/api/models/donors/starter"))["definition"]["dimensions"]


def test_datasets_refreshed_before_models_existed_still_work(
    client_for: ClientFor, source: dict[str, Any], db: Session
) -> None:
    client = client_for()
    load(client, source)
    db.execute(delete(SemanticModel))  # as after upgrading from a version without models
    db.commit()

    listed = {m["dataset"]: m for m in ok(client.get("/api/models"))}
    assert listed["gifts"]["saved"] is False and listed["gifts"]["isStarter"] is True
    assert (listed["gifts"]["dimensions"], listed["gifts"]["measures"]) == (6, 2)
    model = ok(client.get("/api/models/gifts"))
    assert model["saved"] is False and "total_amount" in model["definition"]["measures"]
    assert query(client, {"dataset": "gifts", "measures": ["gifts.row_count"]})["rows"] == [[2000]]
    # Saving it makes it a normal, edited model.
    assert ok(client.put("/api/models/gifts", json={"yaml": model["yaml"]}))["saved"] is True


def test_query_results_match_hand_written_sql(
    client_for: ClientFor, source: dict[str, Any]
) -> None:
    """The Milestone 3 acceptance check."""
    client, conn = client_for(), source["conn"]
    load(client, source)
    saved = ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    assert saved["isStarter"] is False and len(saved["definition"]["rls"]) == 1

    # 1. Measures by a dimension, filtered and sorted.
    result = query(
        client,
        {
            "dataset": "gifts",
            "dimensions": ["gifts.fund"],
            "measures": [
                "gifts.total_amount",
                "gifts.gift_count",
                "gifts.donors",
                "gifts.avg_gift",
            ],
            "filters": [{"field": "gifts.region", "op": "in", "values": ["VIC", "NSW"]}],
            "segments": ["gifts.large"],
            "sort": [{"field": "gifts.total_amount", "dir": "desc"}],
        },
        includeSql=True,
    )
    expected = conn.execute(
        """SELECT fund, sum(amount), count(*), count(DISTINCT donor_id), sum(amount) / count(*)
           FROM src.gifts WHERE region IN ('VIC', 'NSW') AND amount >= 500
           GROUP BY fund ORDER BY sum(amount) DESC"""
    ).fetchall()
    assert len(expected) == 4
    for row, (fund, total, count, donors, average) in zip(result["rows"], expected, strict=True):
        assert row[:4] == [fund, float(total), count, donors]
        assert row[4] == pytest.approx(float(average))
    assert [c["format"] for c in result["columns"]] == [
        None,
        "currency_aud",
        None,
        None,
        "currency_aud",
    ]
    assert result["sql"].startswith("SELECT ") and "read_parquet" in result["sql"].lower()
    assert result["truncated"] is False and result["rowCount"] == 4

    # 2. Financial quarters (July start) over an absolute range, joined to another dataset.
    result = query(
        client,
        {
            "dataset": "gifts",
            "dimensions": ["donors.tier"],
            "measures": ["gifts.total_amount", "gifts.gift_count"],
            "timeDimension": {
                "field": "gifts.gift_date",
                "grain": "fiscal_quarter",
                "range": {"from": "2025-07-01", "to": "2026-06-30"},
            },
            "sort": [{"field": "gifts.gift_date"}, {"field": "donors.tier"}],
        },
    )
    expected = conn.execute(
        """SELECT d.tier,
                  (date_trunc('quarter', g.gift_date - interval '6 months') + interval '6 months')::date,
                  sum(g.amount), count(*)
           FROM src.gifts g LEFT JOIN src.donors d ON d.id = g.donor_id
           WHERE g.gift_date BETWEEN '2025-07-01' AND '2026-06-30'
           GROUP BY 1, 2 ORDER BY 2, 1 NULLS LAST"""
    ).fetchall()
    assert len(expected) == 12  # four quarters by major, regular and no matching donor
    assert result["rows"] == [
        [tier, quarter.isoformat(), float(total), count] for tier, quarter, total, count in expected
    ]
    assert {row[1] for row in result["rows"]} == {
        "2025-07-01",
        "2025-10-01",
        "2026-01-01",
        "2026-04-01",
    }
    assert (
        sum(row[3] for row in result["rows"])
        == conn.execute(
            "SELECT count(*) FROM src.gifts WHERE gift_date BETWEEN '2025-07-01' AND '2026-06-30'"
        ).fetchone()[0]
    )

    # 3. A relative financial-year range, resolved in the workspace calendar.
    this_fy = query(
        client,
        {"dataset": "gifts", "measures": ["gifts.gift_count"],
         "timeDimension": {"field": "gifts.gift_date", "range": {"relative": "last_fiscal_year"}}},
        includeSql=True,
    )  # fmt: skip
    today = client.app.state.settings.today()  # type: ignore[attr-defined]
    start_year = today.year - 1 if today.month >= 7 else today.year - 2
    expected_count = conn.execute(
        "SELECT count(*) FROM src.gifts WHERE gift_date >= %s AND gift_date < %s",
        (f"{start_year}-07-01", f"{start_year + 1}-07-01"),
    ).fetchone()[0]
    assert this_fy["rows"] == [[expected_count]]
    assert f"'{start_year}-07-01'" in this_fy["sql"]


def test_rls_filters_rows_per_user_attribute(client_for: ClientFor, source: dict[str, Any]) -> None:
    client, conn = client_for(), source["conn"]
    load(client, source)
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    by_region = {"dataset": "gifts", "dimensions": ["gifts.region"], "measures": ["gifts.total_amount"],
                 "sort": [{"field": "gifts.region"}]}  # fmt: skip
    totals = {
        region: float(total)
        for region, total in conn.execute("SELECT region, sum(amount) FROM src.gifts GROUP BY 1")
    }

    def seen(**preview: Any) -> dict[str, float]:
        result = (
            query(client, by_region, previewAs=preview) if preview else query(client, by_region)
        )
        return {row[0]: row[1] for row in result["rows"]}

    assert seen() == totals  # the built-in admin is not restricted
    assert seen(role="viewer", attributes={"regions": ["VIC"]}) == {"VIC": totals["VIC"]}
    assert seen(role="editor", attributes={"regions": ["NSW", "QLD"]}) == {
        "NSW": totals["NSW"],
        "QLD": totals["QLD"],
    }
    assert seen(role="viewer", attributes={}) == {}
    assert seen(role="viewer", attributes={"regions": []}) == {}
    assert seen(role="viewer", attributes={"regions": ["VIC' OR '1'='1"]}) == {}
    assert seen(role="admin", attributes={}) == totals
    assert (
        client.post(
            "/api/query", json={"query": by_region, "previewAs": {"role": "root"}}
        ).status_code
        == 422
    )

    # A user's filter cannot reach outside their rows.
    nsw = {**by_region, "filters": [{"field": "gifts.region", "op": "eq", "values": ["NSW"]}]}
    assert (
        query(client, nsw, previewAs={"role": "viewer", "attributes": {"regions": ["VIC"]}})["rows"]
        == []
    )

    # Results are cached per RLS context, never shared between users.
    vic = {"role": "viewer", "attributes": {"regions": ["VIC"]}}
    assert query(client, by_region, previewAs=vic)["cacheHit"] is True
    assert (
        query(client, by_region, previewAs={"role": "viewer", "attributes": {"regions": ["QLD"]}})[
            "cacheHit"
        ]
        is False
    )


def test_cache_follows_snapshots(client_for: ClientFor, source: dict[str, Any]) -> None:
    client = client_for()
    ids = load(client, source)
    count = {"dataset": "gifts", "measures": ["gifts.row_count"]}
    first = query(client, count)
    again = query(client, count)
    assert (first["cacheHit"], again["cacheHit"]) == (False, True) and again["rows"] == [[2000]]

    source["conn"].execute("DELETE FROM src.gifts WHERE id > 1500")
    assert query(client, count)["rows"] == [[2000]]  # still the old snapshot, by design
    refresh(client, ids["gifts"])
    fresh = query(client, count)
    assert fresh["cacheHit"] is False and fresh["rows"] == [[1500]]
    assert fresh["snapshots"]["gifts"] != first["snapshots"]["gifts"]

    # The row cap and truncation flag.
    capped = query(
        client_for(query_max_rows=10),
        {"dataset": "gifts", "dimensions": ["gifts.id"], "limit": 500},
    )
    assert capped["rowCount"] == 10 and capped["truncated"] is True


def test_model_yaml_round_trip_and_validation(
    client_for: ClientFor, source: dict[str, Any], db: Session
) -> None:
    client = client_for()
    load(client, source)

    checked = ok(client.post("/api/models/gifts/validate", json={"yaml": MODEL_YAML}))
    assert checked == {"valid": True, "problems": []}
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    exported = client.get("/api/models/gifts/export")
    assert exported.headers["content-type"].startswith("application/yaml")
    assert 'filename="gifts.yml"' in exported.headers["content-disposition"]
    assert models.from_yaml(exported.text) == models.from_yaml(MODEL_YAML)
    # The author's order of fields survives storage and export.
    measures = [
        line.strip().split(":")[0] for line in exported.text.split("measures:")[1].splitlines()
    ]
    assert [m for m in measures if m in ("total_amount", "gift_count", "donors", "avg_gift")] == [
        "total_amount", "gift_count", "donors", "avg_gift",
    ]  # fmt: skip
    refs = [
        f["ref"] for f in ok(client.get("/api/models/gifts"))["fields"] if f["kind"] == "dimension"
    ]
    assert refs[:4] == ["gifts.fund", "gifts.region", "gifts.gift_date", "gifts.band"]
    # Saving the same fields in a different order is a real change and is stored.
    reordered = MODEL_YAML.replace(
        "  total_amount: { agg: sum, column: amount, format: currency_aud }\n  gift_count: { agg: count }\n",
        "  gift_count: { agg: count }\n  total_amount: { agg: sum, column: amount, format: currency_aud }\n",
    )
    assert reordered != MODEL_YAML
    stored = ok(client.put("/api/models/gifts", json={"yaml": reordered}))
    assert list(stored["definition"]["measures"])[:2] == ["gift_count", "total_amount"]
    ok(client.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    exported = client.get("/api/models/gifts/export")
    # Importing the export changes nothing.
    again = ok(client.put("/api/models/gifts", json={"yaml": exported.text}))
    assert models.from_yaml(again["yaml"]) == models.from_yaml(MODEL_YAML)
    assert (
        db.scalars(select(AuditLog.action).where(AuditLog.action == "model.updated")).all()
        == ["model.updated"] * 4
    )

    bad = MODEL_YAML.replace("column: amount", "column: Amount").replace(
        'amount >= 500"', '(SELECT 1)"'
    )
    response = client.put("/api/models/gifts", json={"yaml": bad})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_model"
    paths = {d["path"]: d["message"] for d in response.json()["error"]["details"]}
    assert (
        paths["/measures/total_amount/column"]
        == "There is no column called 'Amount'. Did you mean 'amount'?"
    )
    assert "Subqueries" in paths["/segments/large/filter"]
    assert ok(client.post("/api/models/gifts/validate", json={"yaml": bad}))["valid"] is False
    # The saved model is unchanged by the failed save.
    assert models.from_yaml(ok(client.get("/api/models/gifts"))["yaml"]) == models.from_yaml(
        MODEL_YAML
    )

    broken = client.put("/api/models/gifts", json={"yaml": "dimensions:\n  a: [unclosed"})
    assert broken.status_code == 422 and broken.json()["error"]["code"] == "invalid_yaml"
    assert "line" in broken.json()["error"]["message"]
    assert client.put("/api/models/gifts", json={"yaml": "- just\n- a list"}).status_code == 422
    assert (
        client.put(
            "/api/models/gifts", json={"yaml": "!!python/object/apply:os.system ['id']"}
        ).status_code
        == 422
    )
    assert (
        client.put("/api/models/donors", json={"yaml": MODEL_YAML}).status_code == 422
    )  # wrong dataset
    assert client.put("/api/models/gifts", json={}).status_code == 422
    assert client.put("/api/models/nope", json={"definition": {}}).status_code == 404


def test_query_errors_are_precise(client_for: ClientFor, source: dict[str, Any]) -> None:
    client = client_for()
    connection = connect(client, "postgres", source["config"])
    body = {"name": "gifts", "connectionId": connection, "stream": "src.gifts", "refresh": False}
    ok(client.post("/api/datasets", json=body), 201)

    def error(body: Any) -> dict[str, Any]:
        response = client.post("/api/query", json={"query": body})
        assert response.status_code >= 400, response.text
        return {"status": response.status_code, **response.json()["error"]}

    assert error({"dataset": "gifts", "measures": ["gifts.row_count"]})["code"] == "no_data"
    assert error({"dataset": "missing", "measures": ["missing.x"]})["status"] == 404
    run_refresh = ok(client.get("/api/datasets"))[0]
    refresh(client, run_refresh["id"])

    schema = error(
        {
            "dataset": "gifts",
            "measures": ["gifts.row_count; DROP TABLE x"],
            "limit": 0,
            "sql": "SELECT 1",
        }
    )
    assert schema["status"] == 422 and schema["code"] == "invalid_query"
    assert {d["path"] for d in schema["details"]} == {"/", "/measures/0", "/limit"}
    semantic = error({"dataset": "gifts", "dimensions": ["gifts.nope"], "measures": ["gifts.fund"]})
    assert {d["path"] for d in semantic["details"]} == {"/dimensions/0", "/measures/0"}
    assert error({"dataset": "gifts"})["status"] == 422
    assert error("SELECT * FROM gifts")["status"] == 422
    # Nothing leaks about the server in an error.
    assert "/data" not in str(schema) and "/data" not in str(semantic)
