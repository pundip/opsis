"""Milestone 2: connectors, encrypted connections, datasets, refresh jobs, snapshots."""

from __future__ import annotations

import io
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pymysql
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opsis.connectors.registry import ConnectorRegistry
from opsis.db.models import AuditLog, Connection, Job
from opsis.jobs import queue, worker
from opsis.services import crypto
from opsis.settings import Settings
from opsis.storage import snapshots
from tests.conftest import make_settings

ClientFor = Callable[..., TestClient]
MYSQL_HOST = os.environ.get("OPSIS_TEST_MYSQL_HOST")
needs_mysql = pytest.mark.skipif(not MYSQL_HOST, reason="no test MySQL server")


# ---- helpers ----------------------------------------------------------------------


def settings_of(client: TestClient) -> Settings:
    app: Any = client.app
    settings: Settings = app.state.settings
    return settings


def run_jobs(client: TestClient) -> None:
    """Run queued jobs the way the worker container does."""
    app: Any = client.app
    worker.register_handlers()
    while True:
        with app.state.session_factory() as db:
            if not worker.run_one(db, "test-worker", settings_of(client)):
                return


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def make_dataset(
    client: TestClient, connection_id: str, stream: str, **extra: Any
) -> dict[str, Any]:
    body = {
        "name": extra.pop("name", "t"),
        "connectionId": connection_id,
        "stream": stream,
        **extra,
    }
    dataset = ok(client.post("/api/datasets", json=body), 201)
    assert dataset["lastJob"]["status"] == "queued"
    return dataset


def refresh(client: TestClient, dataset_id: str, **body: Any) -> dict[str, Any]:
    ok(client.post(f"/api/datasets/{dataset_id}/refresh", json=body), 202)
    run_jobs(client)
    return ok(client.get(f"/api/datasets/{dataset_id}"))


def rows_of(client: TestClient, dataset_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    data = ok(client.get(f"/api/datasets/{dataset_id}/preview?limit={limit}"))
    names = [c["name"] for c in data["columns"]]
    return [dict(zip(names, row, strict=True)) for row in data["rows"]]


def upload(client: TestClient, name: str, content: bytes) -> dict[str, Any]:
    return ok(client.post("/api/uploads", files={"file": (name, content)}), 201)


def snapshot_dirs(client: TestClient, dataset_id: str) -> list[str]:
    folder = settings_of(client).data_dir / "datasets" / dataset_id
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ---- sources ----------------------------------------------------------------------


@pytest.fixture
def pg_source(migrated_db: Settings) -> Iterator[dict[str, Any]]:
    """A source schema in the test Postgres, with one table of every mapped type."""
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
            """CREATE TABLE src.orders (
                 id integer PRIMARY KEY, customer text NOT NULL, amount numeric(10,2),
                 ratio double precision, paid boolean, ordered_on date,
                 updated_at timestamptz NOT NULL, meta jsonb, ref uuid)"""
        )
        conn.execute(
            """INSERT INTO src.orders VALUES
               (1, 'Ana',  10.50, 0.5, true,  '2026-01-05', '2026-01-05 10:00+00', '{"a": 1}',
                '11111111-1111-1111-1111-111111111111'),
               (2, 'Ben',  20.00, 1.5, false, '2026-01-06', '2026-01-06 10:00+00', NULL, NULL),
               (3, 'Cy; DROP TABLE x', NULL, NULL, NULL, NULL, '2026-01-07 10:00+00', NULL, NULL)"""
        )
        conn.execute('CREATE TABLE src."Odd ""Name" ("a b" integer, "select" text)')
        conn.execute("""INSERT INTO src."Odd ""Name" VALUES (1, 'x')""")
        yield {"config": config, "conn": conn}
        conn.execute("DROP SCHEMA IF EXISTS src CASCADE")


@pytest.fixture
def mysql_source() -> Iterator[dict[str, Any]]:
    conn = pymysql.connect(host=MYSQL_HOST, user="root", password="root-sample-pw", autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS opsis_test_src")
        cur.execute("CREATE DATABASE opsis_test_src")
        cur.execute(
            """CREATE TABLE opsis_test_src.orders (
                 id INT PRIMARY KEY, customer VARCHAR(50) NOT NULL, amount DECIMAL(10,2),
                 ratio DOUBLE, paid TINYINT(1), ordered_on DATE, updated_at DATETIME NOT NULL,
                 meta JSON, big BIGINT)"""
        )
        cur.execute(
            """INSERT INTO opsis_test_src.orders VALUES
               (1, 'Ana', 10.50, 0.5, 1, '2026-01-05', '2026-01-05 10:00:00', '{"a": 1}', 9007199254740993),
               (2, 'Ben', 20.00, 1.5, 0, '2026-01-06', '2026-01-06 10:00:00', NULL, NULL),
               (3, 'Cy',  NULL,  NULL, NULL, NULL,     '2026-01-07 10:00:00', NULL, NULL)"""
        )
    config = {
        "host": MYSQL_HOST,
        "database": "opsis_test_src",
        "user": "root",
        "password": "root-sample-pw",
    }
    yield {"config": config, "conn": conn}
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS opsis_test_src")
    conn.close()


def connect(client: TestClient, connector: str, config: dict[str, Any], name: str = "src") -> str:
    body = {"name": name, "connectorId": connector, "config": config}
    return str(ok(client.post("/api/connections", json=body), 201)["id"])


# ---- registry and secrets ---------------------------------------------------------


def test_registry_finds_builtin_and_dropin_connectors(tmp_path: Path) -> None:
    assert {c.id for c in ConnectorRegistry().all()} == {"file", "mysql", "postgres"}

    plugin = tmp_path / "connectors" / "demo"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        "class Demo:\n"
        "    id, name, version = 'demo', 'Demo', '0.1.0'\n"
        "    @classmethod\n"
        "    def config_schema(cls): return {'type': 'object', 'properties': {}}\n"
        "    def __init__(self, config): pass\n"
        "    def test_connection(self): pass\n"
        "    def discover(self): return []\n"
        "    def read(self, stream, mode, state, options=None): pass\n"
        "    def close(self): pass\n"
        "CONNECTOR = Demo\n"
    )
    broken = tmp_path / "connectors" / "broken"
    broken.mkdir()
    (broken / "__init__.py").write_text("raise RuntimeError('boom')\n")
    registry = ConnectorRegistry(tmp_path)
    assert registry.get("demo") is not None and registry.get("broken") is None
    assert {"file", "mysql", "postgres"} <= {c.id for c in registry.all()}


def test_secret_encryption(tmp_path: Path) -> None:
    with_key = make_settings(data_dir=tmp_path / "a")
    token = crypto.encrypt(with_key, {"password": "hunter2"})
    assert token and "hunter2" not in token
    assert crypto.decrypt(with_key, token) == {"password": "hunter2"}
    assert crypto.encrypt(with_key, {}) is None and crypto.decrypt(with_key, None) == {}

    # Local mode without ENCRYPTION_KEY: a key is generated once and reused.
    local = make_settings(data_dir=tmp_path / "b", encryption_key="")
    token = crypto.encrypt(local, {"password": "x"})
    key_file = tmp_path / "b" / "secrets" / "encryption.key"
    assert key_file.is_file() and oct(key_file.stat().st_mode & 0o777) == "0o600"
    crypto._cache.clear()
    assert crypto.decrypt(local, token) == {"password": "x"}

    with pytest.raises(crypto.SecretsError, match="ENCRYPTION_KEY"):
        crypto.decrypt(with_key, token)  # a different key cannot read it


def test_connectors_endpoint_lists_schemas(client_for: ClientFor) -> None:
    connectors = {c["id"]: c for c in ok(client_for().get("/api/connectors"))}
    assert set(connectors) == {"file", "mysql", "postgres"}
    assert connectors["mysql"]["configSchema"]["properties"]["password"]["x-secret"] is True
    assert connectors["file"]["configSchema"]["properties"]["file"]["x-upload"] is True


# ---- connections ------------------------------------------------------------------


def test_connection_secrets_are_encrypted_and_never_returned(
    client_for: ClientFor, db: Session, pg_source: dict[str, Any]
) -> None:
    client = client_for()
    config = pg_source["config"]
    password = config["password"]
    created = ok(
        client.post(
            "/api/connections", json={"name": "PG", "connectorId": "postgres", "config": config}
        ),
        201,
    )
    assert "password" not in created["config"] and created["secretFields"] == ["password"]
    for path in ("/api/connections", f"/api/connections/{created['id']}"):
        assert password not in client.get(path).text

    row = db.scalar(select(Connection))
    assert row is not None and row.secrets and password not in row.secrets
    assert "password" not in row.config
    assert crypto.decrypt(settings_of(client), row.secrets) == {"password": password}

    assert ok(client.post(f"/api/connections/{created['id']}/test")) == {
        "ok": True,
        "message": "Connected.",
    }
    # Editing without retyping the password keeps the saved one.
    without_password = {k: v for k, v in config.items() if k != "password"}
    updated = ok(
        client.patch(
            f"/api/connections/{created['id']}", json={"name": "PG2", "config": without_password}
        )
    )
    assert updated["name"] == "PG2" and updated["secretFields"] == ["password"]
    assert ok(client.post(f"/api/connections/{created['id']}/test"))["ok"] is True
    # Testing form values before saving, reusing the saved secret.
    probe = {"connectorId": "postgres", "config": without_password, "connectionId": created["id"]}
    assert ok(client.post("/api/connections/test", json=probe))["ok"] is True

    wrong = ok(
        client.post(
            "/api/connections/test",
            json={"connectorId": "postgres", "config": {**config, "password": "nope-nope"}},
        )
    )
    assert wrong["ok"] is False and "nope-nope" not in wrong["message"]

    actions = db.scalars(select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions[-2:] == ["connection.created", "connection.updated"]
    details = str(db.scalars(select(AuditLog.detail)).all())
    assert password not in details


def test_connection_validation_reports_paths(client_for: ClientFor) -> None:
    client = client_for()
    bad = client.post(
        "/api/connections",
        json={
            "name": "x",
            "connectorId": "mysql",
            "config": {"host": "h", "port": "abc", "zzz": 1},
        },
    )
    assert bad.status_code == 422
    error = bad.json()["error"]
    assert error["code"] == "invalid_config"
    paths = {d["path"] for d in error["details"]}
    assert "/config/port" in paths and "/config/" in paths  # bad type; missing and extra keys
    assert (
        client.post(
            "/api/connections", json={"name": "x", "connectorId": "nope", "config": {}}
        ).status_code
        == 404
    )


# ---- file connector ---------------------------------------------------------------

CSV = (
    "id,name,amount,when,active,joined\n"
    "1,Ana,10.5,2026-01-05,true,2026-01-05 10:00:00\n"
    '2,"Ben, Jr",20,2026-01-06,false,2026-01-06 11:30:00\n'
    "3,Cy,,,,\n"
)


def test_csv_upload_to_preview(client_for: ClientFor) -> None:
    client = client_for()
    uploaded = upload(client, "My Gifts 2026.csv", CSV.encode())
    assert uploaded["filename"] == "My Gifts 2026.csv" and uploaded["sizeBytes"] == len(CSV)
    connection = connect(client, "file", {"file": uploaded["file"]})
    assert ok(client.post(f"/api/connections/{connection}/test"))["ok"] is True

    streams = ok(client.get(f"/api/connections/{connection}/streams"))
    assert [s["name"] for s in streams] == ["My Gifts 2026"]
    assert streams[0]["suggestedName"] == "my_gifts_2026"
    assert streams[0]["supportedModes"] == ["full"]
    types = {c["name"]: c["type"] for c in streams[0]["columns"]}
    assert types == {
        "id": "integer",
        "name": "string",
        "amount": "float",
        "when": "date",
        "active": "boolean",
        "joined": "timestamp",
    }

    dataset = make_dataset(client, connection, "My Gifts 2026", name="gifts")
    run_jobs(client)
    dataset = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert dataset["lastJob"]["status"] == "succeeded", dataset["lastJob"]
    assert dataset["snapshot"]["rowCount"] == 3
    assert rows_of(client, dataset["id"]) == [
        {"id": 1, "name": "Ana", "amount": 10.5, "when": "2026-01-05", "active": True,
         "joined": "2026-01-05T10:00:00"},
        {"id": 2, "name": "Ben, Jr", "amount": 20.0, "when": "2026-01-06", "active": False,
         "joined": "2026-01-06T11:30:00"},
        {"id": 3, "name": "Cy", "amount": None, "when": None, "active": None, "joined": None},
    ]  # fmt: skip
    assert len(rows_of(client, dataset["id"], limit=2)) == 2


def test_excel_upload_has_one_stream_per_sheet(client_for: ClientFor) -> None:
    from datetime import date, datetime

    from openpyxl import Workbook

    book = Workbook()
    first = book.active
    first.title = "Gifts"
    first.append(["Fund", "Amount", "Given", "Count", "Mixed", None])
    first.append(["Library", 10.5, datetime(2026, 1, 5), 1, 1, "x"])
    first.append(["Sport", 20, date(2026, 1, 6), 2, "two", None])
    first.append([None, None, None, None, None, None])
    second = book.create_sheet("Empty")
    third = book.create_sheet("No header")
    third.append([1, "a"])
    assert second is not None
    buffer = io.BytesIO()
    book.save(buffer)

    client = client_for()
    connection = connect(
        client, "file", {"file": upload(client, "book.xlsx", buffer.getvalue())["file"]}
    )
    streams = {s["name"]: s for s in ok(client.get(f"/api/connections/{connection}/streams"))}
    assert set(streams) == {"Gifts", "No header"}
    assert {c["name"]: c["type"] for c in streams["Gifts"]["columns"]} == {
        "Fund": "string",
        "Amount": "float",
        "Given": "date",
        "Count": "integer",
        "Mixed": "string",
        "column_6": "string",
    }
    dataset = make_dataset(client, connection, "Gifts", name="gifts")
    run_jobs(client)
    assert rows_of(client, dataset["id"]) == [
        {"Fund": "Library", "Amount": 10.5, "Given": "2026-01-05", "Count": 1, "Mixed": "1",
         "column_6": "x"},
        {"Fund": "Sport", "Amount": 20.0, "Given": "2026-01-06", "Count": 2, "Mixed": "two",
         "column_6": None},
    ]  # fmt: skip


def test_upload_rules(client_for: ClientFor) -> None:
    client = client_for(max_upload_mb=1)
    assert client.post("/api/uploads", files={"file": ("x.exe", b"MZ")}).status_code == 422
    assert client.post("/api/uploads", files={"file": ("x.csv", b"")}).status_code == 422
    too_big = client.post("/api/uploads", files={"file": ("x.csv", b"a,b\n" * 600_000)})
    assert too_big.status_code == 413 and "1 MB" in too_big.json()["error"]["message"]
    # Path tricks in the file name are flattened to a plain name inside the uploads folder.
    tricky = upload(client, "../../etc/pass wd.csv", b"a\n1\n")
    assert tricky["filename"] == "pass wd.csv" and ".." not in tricky["file"]
    uploads = settings_of(client).data_dir / "uploads"
    assert [p.name for p in uploads.glob("*/*")] == ["pass wd.csv"]  # failed uploads left nothing

    # A connection cannot point outside the uploads folder.
    escape = connect(client, "file", {"file": "../secrets/encryption.key"}, name="escape")
    result = ok(client.post(f"/api/connections/{escape}/test"))
    assert result["ok"] is False and "not found" in result["message"]


# ---- SQL connectors ---------------------------------------------------------------


def test_postgres_discovery_types_and_full_refresh(
    client_for: ClientFor, pg_source: dict[str, Any]
) -> None:
    client = client_for()
    connection = connect(client, "postgres", pg_source["config"])
    streams = {s["name"]: s for s in ok(client.get(f"/api/connections/{connection}/streams"))}
    orders = streams["src.orders"]
    assert orders["primaryKey"] == ["id"]
    assert orders["supportedModes"] == ["full", "append", "merge"]
    assert orders["cursorCandidates"] == ["id", "amount", "ordered_on", "updated_at"]
    assert {c["name"]: c["type"] for c in orders["columns"]} == {
        "id": "integer",
        "customer": "string",
        "amount": "decimal",
        "ratio": "float",
        "paid": "boolean",
        "ordered_on": "date",
        "updated_at": "timestamp",
        "meta": "json",
        "ref": "string",
    }
    assert "public.users" in streams  # every non-system schema is listed

    dataset = make_dataset(client, connection, "src.orders", name="orders")
    run_jobs(client)
    rows = rows_of(client, dataset["id"])
    assert rows[0] == {
        "id": 1,
        "customer": "Ana",
        "amount": "10.50",
        "ratio": 0.5,
        "paid": True,
        "ordered_on": "2026-01-05",
        "updated_at": "2026-01-05T10:00:00+00:00",  # timestamptz: stored as a UTC instant
        "meta": '{"a": 1}',
        "ref": "11111111-1111-1111-1111-111111111111",
    }
    assert rows[2]["customer"] == "Cy; DROP TABLE x" and rows[2]["amount"] is None

    # Awkward identifiers are quoted, never interpolated raw.
    odd = make_dataset(client, connection, 'src.Odd "Name', name="odd")
    run_jobs(client)
    assert rows_of(client, odd["id"]) == [{"a b": 1, "select": "x"}]

    # Column selection.
    slim = make_dataset(client, connection, "src.orders", name="slim", columns=["id", "customer"])
    run_jobs(client)
    assert rows_of(client, slim["id"])[0] == {"id": 1, "customer": "Ana"}


def test_dataset_validation(client_for: ClientFor, pg_source: dict[str, Any]) -> None:
    client = client_for()
    connection = connect(client, "postgres", pg_source["config"])

    def post(**body: Any) -> Any:
        base = {"name": "orders", "connectionId": connection, "stream": "src.orders"}
        return client.post("/api/datasets", json={**base, **body})

    bad = post(name="Orders 2026", refreshMode="append", cursorField="customer", columns=["nope"])
    assert bad.status_code == 422
    paths = {d["path"] for d in bad.json()["error"]["details"]}
    assert paths == {"/name", "/cursorField", "/columns"}
    assert post(stream="src.missing").status_code == 422
    assert post(stream="src.orders; DROP TABLE src.orders").status_code == 422
    assert post(refreshMode="sideways").status_code == 422
    assert post(refresh=False).status_code == 201
    assert post().status_code == 409  # name taken
    assert pg_source["conn"].execute("SELECT count(*) FROM src.orders").fetchone() == (3,)


def test_append_refresh_adds_only_new_rows(
    client_for: ClientFor, pg_source: dict[str, Any]
) -> None:
    client, conn = client_for(), pg_source["conn"]
    connection = connect(client, "postgres", pg_source["config"])
    dataset = make_dataset(
        client, connection, "src.orders", name="orders", refreshMode="append", cursorField="id"
    )
    run_jobs(client)
    first = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert first["snapshot"]["rowCount"] == 3 and first["snapshot"]["mode"] == "full"

    conn.execute("INSERT INTO src.orders (id, customer, updated_at) VALUES (4, 'Di', now())")
    conn.execute("UPDATE src.orders SET customer = 'CHANGED' WHERE id = 1")  # not picked up
    second = refresh(client, dataset["id"])
    assert second["lastJob"]["status"] == "succeeded", second["lastJob"]
    assert second["snapshot"]["rowCount"] == 4 and second["snapshot"]["mode"] == "append"
    assert [r["customer"] for r in rows_of(client, dataset["id"])] == [
        "Ana",
        "Ben",
        "Cy; DROP TABLE x",
        "Di",
    ]

    # Nothing new: still a new snapshot, same rows.
    third = refresh(client, dataset["id"])
    assert (
        third["snapshot"]["rowCount"] == 4 and third["snapshot"]["id"] != second["snapshot"]["id"]
    )

    # A full rebuild picks up the change and resets the cursor.
    rebuilt = refresh(client, dataset["id"], full=True)
    assert rebuilt["snapshot"]["mode"] == "full"
    assert rows_of(client, dataset["id"])[0]["customer"] == "CHANGED"
    conn.execute("INSERT INTO src.orders (id, customer, updated_at) VALUES (5, 'Ed', now())")
    assert refresh(client, dataset["id"])["snapshot"]["rowCount"] == 5

    # A changed source schema is reported, and the dataset keeps its data.
    conn.execute("ALTER TABLE src.orders ADD COLUMN extra text")
    conn.execute("INSERT INTO src.orders (id, customer, updated_at) VALUES (6, 'Flo', now())")
    failed = refresh(client, dataset["id"])
    assert failed["lastJob"]["status"] == "failed"
    assert "Run a full refresh" in failed["lastJob"]["error"]
    assert failed["snapshot"]["rowCount"] == 5


def test_merge_refresh_updates_rows_by_primary_key(
    client_for: ClientFor, pg_source: dict[str, Any]
) -> None:
    client, conn = client_for(), pg_source["conn"]
    connection = connect(client, "postgres", pg_source["config"])
    dataset = make_dataset(
        client,
        connection,
        "src.orders",
        name="orders",
        refreshMode="merge",
        cursorField="updated_at",
    )
    assert dataset["primaryKey"] == ["id"]  # taken from the source
    run_jobs(client)

    conn.execute(
        "UPDATE src.orders SET customer = 'Ana B', amount = 99, updated_at = now() WHERE id = 1"
    )
    conn.execute("INSERT INTO src.orders (id, customer, updated_at) VALUES (4, 'Di', now())")
    merged = refresh(client, dataset["id"])
    assert merged["lastJob"]["status"] == "succeeded", merged["lastJob"]
    assert merged["snapshot"]["mode"] == "merge" and merged["snapshot"]["rowCount"] == 4
    rows = {r["id"]: r for r in rows_of(client, dataset["id"])}
    assert rows[1]["customer"] == "Ana B" and rows[1]["amount"] == "99.00"
    assert rows[2]["customer"] == "Ben" and rows[4]["customer"] == "Di"

    again = refresh(client, dataset["id"])  # nothing changed
    assert again["snapshot"]["rowCount"] == 4


@needs_mysql
def test_mysql_connect_create_refresh_preview_and_swap(
    client_for: ClientFor, mysql_source: dict[str, Any]
) -> None:
    """The Milestone 2 acceptance path, against a real MySQL server."""
    client = client_for(snapshot_retention=2)
    connection = connect(client, "mysql", mysql_source["config"])
    assert ok(client.post(f"/api/connections/{connection}/test"))["ok"] is True

    streams = ok(client.get(f"/api/connections/{connection}/streams"))
    orders = next(s for s in streams if s["name"] == "orders")
    assert orders["primaryKey"] == ["id"] and "merge" in orders["supportedModes"]
    assert {c["name"]: c["type"] for c in orders["columns"]} == {
        "id": "integer",
        "customer": "string",
        "amount": "decimal",
        "ratio": "float",
        "paid": "boolean",
        "ordered_on": "date",
        "updated_at": "timestamp",
        "meta": "json",
        "big": "integer",
    }

    dataset = make_dataset(client, connection, "orders", name="orders")
    run_jobs(client)
    first = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert first["lastJob"]["status"] == "succeeded", first["lastJob"]
    assert first["snapshot"]["rowCount"] == 3
    rows = rows_of(client, dataset["id"])
    assert rows[0] == {
        "id": 1,
        "customer": "Ana",
        "amount": "10.50",
        "ratio": 0.5,
        "paid": True,
        "ordered_on": "2026-01-05",
        "updated_at": "2026-01-05T10:00:00",
        "meta": '{"a": 1}',
        "big": 9007199254740993,
    }
    assert rows[2]["amount"] is None and rows[2]["paid"] is None

    # A second refresh makes a new snapshot and moves the pointer to it.
    with mysql_source["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO opsis_test_src.orders (id, customer, updated_at) VALUES (4, 'Di', NOW())"
        )
    second = refresh(client, dataset["id"])
    assert second["snapshot"]["id"] != first["snapshot"]["id"]
    assert second["snapshot"]["rowCount"] == 4 and len(rows_of(client, dataset["id"])) == 4
    listed = ok(client.get(f"/api/datasets/{dataset['id']}/snapshots"))
    assert [s["current"] for s in listed] == [True, False]
    assert set(snapshot_dirs(client, dataset["id"])) == {
        first["snapshot"]["id"],
        second["snapshot"]["id"],
    }

    # Retention keeps the newest two.
    third = refresh(client, dataset["id"])
    assert set(snapshot_dirs(client, dataset["id"])) == {
        second["snapshot"]["id"],
        third["snapshot"]["id"],
    }
    assert len(ok(client.get(f"/api/datasets/{dataset['id']}/snapshots"))) == 2

    # A failed refresh leaves the current snapshot and its data exactly as they were.
    with mysql_source["conn"].cursor() as cur:
        cur.execute("RENAME TABLE opsis_test_src.orders TO opsis_test_src.orders_gone")
    failed = refresh(client, dataset["id"])
    assert failed["lastJob"]["status"] == "failed"
    assert (
        "orders" in failed["lastJob"]["error"]
        and "root-sample-pw" not in failed["lastJob"]["error"]
    )
    assert failed["snapshot"]["id"] == third["snapshot"]["id"]
    assert len(rows_of(client, dataset["id"])) == 4
    assert not [d for d in snapshot_dirs(client, dataset["id"]) if d.endswith(".tmp")]

    jobs = ok(client.get(f"/api/jobs?datasetId={dataset['id']}"))
    assert [j["status"] for j in jobs] == ["failed", "succeeded", "succeeded", "succeeded"]
    assert ok(client.get(f"/api/jobs/{jobs[0]['id']}"))["error"] == failed["lastJob"]["error"]

    # Deleting: the connection is protected while in use; the dataset takes its files with it.
    assert client.delete(f"/api/connections/{connection}").status_code == 409
    assert client.delete(f"/api/datasets/{dataset['id']}").status_code == 204
    assert snapshot_dirs(client, dataset["id"]) == []
    assert client.delete(f"/api/connections/{connection}").status_code == 204


@needs_mysql
def test_mysql_incremental_and_encrypted_transport(
    client_for: ClientFor, mysql_source: dict[str, Any]
) -> None:
    client = client_for()
    connection = connect(client, "mysql", {**mysql_source["config"], "ssl": "required"})
    assert ok(client.post(f"/api/connections/{connection}/test"))["ok"] is True
    dataset = make_dataset(
        client, connection, "orders", name="orders", refreshMode="merge", cursorField="updated_at"
    )
    run_jobs(client)
    with mysql_source["conn"].cursor() as cur:
        cur.execute(
            "UPDATE opsis_test_src.orders SET customer = 'Ben B', updated_at = '2026-02-01' WHERE id = 2"
        )
        cur.execute(
            "INSERT INTO opsis_test_src.orders (id, customer, updated_at) VALUES (4, 'Di', '2026-02-02')"
        )
    merged = refresh(client, dataset["id"])
    assert merged["lastJob"]["status"] == "succeeded", merged["lastJob"]
    assert merged["snapshot"]["mode"] == "merge"
    assert sorted((r["id"], r["customer"]) for r in rows_of(client, dataset["id"])) == [
        (1, "Ana"),
        (2, "Ben B"),
        (3, "Cy"),
        (4, "Di"),
    ]


# ---- jobs -------------------------------------------------------------------------


def test_one_active_refresh_per_dataset_and_preview_before_data(client_for: ClientFor) -> None:
    client = client_for()
    connection = connect(client, "file", {"file": upload(client, "a.csv", b"x\n1\n")["file"]})
    dataset = make_dataset(client, connection, "a", name="a")
    waiting = client.get(f"/api/datasets/{dataset['id']}/preview")
    assert waiting.status_code == 409 and waiting.json()["error"]["code"] == "no_data"
    again = ok(client.post(f"/api/datasets/{dataset['id']}/refresh"), 202)
    assert again["id"] == dataset["lastJob"]["id"]  # the queued job is reused
    run_jobs(client)
    assert len(ok(client.get("/api/jobs"))) == 1


def test_interrupted_jobs_and_temp_folders_are_cleaned_up(
    client_for: ClientFor, db: Session
) -> None:
    client = client_for()
    settings = settings_of(client)
    job = queue.enqueue(db, "dataset.refresh", {"dataset_id": "x"})
    assert queue.claim_next(db, "dead-worker") is not None
    leftover = settings.data_dir / "datasets" / "d1" / "s1.tmp"
    leftover.mkdir(parents=True)
    keep = settings.data_dir / "datasets" / "d1" / "s0"
    keep.mkdir()

    assert queue.fail_stale(db) == 1
    snapshots.sweep_temp(settings)
    db.refresh(job)
    assert job.status == "failed" and "Interrupted" in (job.error or "")
    assert not leftover.exists() and keep.exists()


def test_inline_runner_runs_jobs_inside_the_api(client_for: ClientFor) -> None:
    client = client_for(job_runner="inline")
    connection = connect(
        client, "file", {"file": upload(client, "a.csv", b"x,y\n1,2\n3,4\n")["file"]}
    )
    dataset = make_dataset(client, connection, "a", name="a")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = ok(client.get(f"/api/datasets/{dataset['id']}"))
        if current["lastJob"]["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)
    assert current["lastJob"]["status"] == "succeeded", current["lastJob"]
    assert rows_of(client, dataset["id"]) == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
    app: Any = client.app
    with sessionmaker(app.state.session_factory.kw["bind"])() as session:
        assert session.scalar(select(Job.locked_by)).startswith("inline:")
