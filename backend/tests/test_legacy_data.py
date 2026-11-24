"""Legacy data: values that do not fit their column's type never fail a read.

They are stored as NULL and reported per column (reason, count, samples);
invalid dates follow the connection's setting; text columns get suggested
types; a dataset's column types can be set by hand. Covers the shared
conversion layer and every connector: MySQL (a MyISAM table in a non-strict
sql_mode), PostgreSQL, CSV, Excel, Google Sheets and Salesforce.
"""

from __future__ import annotations

import io
import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pyarrow as pa
import pymysql
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.connectors.base import ColumnSpec, ColumnType, ConnectorError, ReadOptions, RefreshMode
from opsis.connectors.convert import Conversion, as_text, check_format, invalid_date, suggest
from opsis.connectors.mysql import column_spec
from opsis.db.models import AuditLog, Dataset, Job
from opsis.services import datasets
from opsis.settings import Settings
from tests.test_ingestion import (
    connect,
    make_dataset,
    ok,
    refresh,
    rows_of,
    run_jobs,
    settings_of,
    upload,
)
from tests.test_mcp import Mcp, make_user

ClientFor = Callable[..., TestClient]
MYSQL_HOST = os.environ.get("OPSIS_TEST_MYSQL_HOST")
needs_mysql = pytest.mark.skipif(not MYSQL_HOST, reason="no test MySQL server")

T = ColumnType


def col(name: str, kind: ColumnType, **extra: Any) -> ColumnSpec:
    return ColumnSpec(name=name, type=kind, **extra)


def emptied(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["column"]: entry for entry in report["emptied"]}


def reasons(entry: dict[str, Any]) -> dict[str, int]:
    return {r["reason"]: r["count"] for r in entry["reasons"]}


# ---- the conversion layer ------------------------------------------------------------------


def test_zero_partial_and_impossible_dates_become_null_and_are_counted() -> None:
    conversion = Conversion(table="a_codes")
    columns = [
        col("d", T.DATE, source_type="date"),
        col("ts", T.TIMESTAMP, source_type="datetime"),
    ]
    rows = [
        (date(2020, 1, 1), datetime(2020, 1, 1, 9, 30)),
        ("0000-00-00", "0000-00-00 00:00:00"),  # zero
        ("2020-05-00", "2020-05-00 10:00:00"),  # zero day
        ("2020-00-10", "2020-00-10 10:00:00"),  # zero month
        ("2021-02-30", "2021-02-30 10:00:00"),  # no such day
        (None, None),  # a NULL is not counted
    ]
    batch = conversion.batch(columns, rows)
    assert batch.schema.field("d").type == pa.date32()
    assert batch.column("d").to_pylist() == [date(2020, 1, 1), None, None, None, None, None]
    assert batch.column("ts").to_pylist() == [datetime(2020, 1, 1, 9, 30), *[None] * 5]
    report = conversion.report()
    assert report["emptiedValues"] == 8
    found = emptied(report)
    assert found["d"]["count"] == 4 and found["d"]["sourceType"] == "date"
    assert reasons(found["d"]) == {
        "zero date": 1,
        "partial date (zero year, month or day)": 2,
        "impossible date": 1,
    }
    assert found["ts"]["samples"] == [
        "0000-00-00 00:00:00",
        "2020-05-00 10:00:00",
        "2020-00-10 10:00:00",
        "2021-02-30 10:00:00",
    ]
    json.dumps(report)  # stored as JSON with the snapshot and the job


def test_invalid_date_reasons() -> None:
    assert invalid_date("0000-00-00") == "zero date"
    assert invalid_date("00/00/0000") == "zero date"
    assert invalid_date("2020-05-00") == invalid_date("0000-05-01") != None  # noqa: E711
    assert invalid_date("2021-02-29") == "impossible date"
    assert invalid_date("infinity") == invalid_date("-infinity") == "infinite date"
    assert invalid_date("0044-03-15 BC") == "date before year 1"
    assert invalid_date("10000-01-01 00:00:00") == "year after 9999"
    assert invalid_date("2024-02-29") is None and invalid_date("hello") is None


def test_garbage_in_number_columns_is_counted_not_fatal() -> None:
    conversion = Conversion(table="t")
    columns = [
        col("n", T.INTEGER, source_type="varchar(20)"),
        col("x", T.FLOAT),
        col("money", T.DECIMAL, scale=2, source_type="decimal(10,2)"),
        col("flag", T.BOOLEAN),
    ]
    rows = [
        ("12", "1.5", Decimal("10.50"), "yes"),
        ("1,234", "abc", "12.345", "N"),
        ("abc", "", Decimal("NaN"), "maybe"),
        (1.5, "1e3", "1" * 40, 2),
        (2**64, None, "-", b"\x00"),
        (" 7 ", float("nan"), 3, b"\x01"),
    ]
    batch = conversion.batch(columns, rows)
    assert batch.column("n").to_pylist() == [12, 1234, None, None, None, 7]
    assert batch.column("x").to_pylist()[:4] == [1.5, None, None, 1000.0]
    assert batch.column("money").to_pylist() == [
        Decimal("10.50"),
        Decimal("12.35"),
        None,
        None,
        None,
        Decimal("3.00"),
    ]
    assert batch.column("flag").to_pylist() == [True, False, None, True, False, True]
    found = emptied(conversion.report())
    assert reasons(found["n"]) == {
        "not a number": 1,
        "not a whole number": 1,  # 1.5 is not truncated to 1
        "too large for a 64-bit integer": 1,
    }
    assert found["n"]["samples"] == ["abc", "1.5", "18446744073709551616"]
    assert reasons(found["x"]) == {"not a number": 2}
    assert reasons(found["money"]) == {"not a number": 2, "too large for a decimal": 1}
    assert reasons(found["flag"]) == {"not true or false": 1}


def test_a_fraction_in_an_integer_column_is_never_rounded() -> None:
    conversion = Conversion()
    batch = conversion.batch([col("n", T.INTEGER)], [(1,), (2.0,), (2.5,)])
    assert batch.column("n").to_pylist() == [1, 2, None]
    assert conversion.report()["emptiedValues"] == 1


def test_mysql_values_as_text_and_numbers() -> None:
    conversion = Conversion()
    columns = [
        col("slot", T.STRING, source_type="time"),
        col("raw", T.STRING, source_type="varbinary(4)"),
        col("bits", T.INTEGER, source_type="bit(8)"),
        col("wide", T.DECIMAL, scale=0, source_type="bit(64)"),
        col("counter", T.DECIMAL, scale=0, source_type="bigint unsigned"),
        col("meta", T.JSON),
    ]
    rows = [
        (timedelta(hours=838, minutes=59, seconds=59), b"abc", b"\x05", b"\xff" * 8, 2**64 - 1, {"a": [1]}),
        (-timedelta(hours=1, minutes=30), b"\xff\x00", b"\x00", b"\x00" * 8, 0, '{"b": 2}'),
    ]  # fmt: skip
    batch = conversion.batch(columns, rows)
    assert batch.column("slot").to_pylist() == ["838:59:59", "-01:30:00"]
    assert batch.column("raw").to_pylist() == ["abc", "0xff00"]  # binary is shown, not mangled
    assert batch.column("bits").to_pylist() == [5, 0]
    assert batch.column("wide").to_pylist() == [Decimal(2**64 - 1), Decimal(0)]
    assert batch.column("counter").to_pylist() == [Decimal(2**64 - 1), Decimal(0)]
    assert batch.column("meta").to_pylist() == ['{"a": [1]}', '{"b": 2}']
    assert conversion.report()["emptiedValues"] == 0
    assert as_text(datetime(2020, 1, 2, 3, 4, 5)) == "2020-01-02 03:04:05"


def test_samples_are_capped_at_five_distinct_values() -> None:
    conversion = Conversion()
    values = [("0000-00-00",)] * 3 + [(f"2020-0{m}-00",) for m in range(1, 9)]
    conversion.batch([col("d", T.DATE)], values)
    entry = emptied(conversion.report())["d"]
    assert entry["count"] == 11
    assert entry["samples"] == [
        "0000-00-00",
        "2020-01-00",
        "2020-02-00",
        "2020-03-00",
        "2020-04-00",
    ]


def test_invalid_dates_can_stop_the_refresh_or_be_kept_as_text() -> None:
    columns = [col("end_date", T.TIMESTAMP, source_type="datetime"), col("n", T.INTEGER)]
    rows = [(datetime(2020, 1, 1), 1), ("0000-00-00 00:00:00", 2)]

    stop = Conversion(table="a_codes", invalid_dates="fail")
    with pytest.raises(ConnectorError) as raised:
        stop.batch(columns, rows)
    message = str(raised.value)
    assert message.startswith(
        "Column 'end_date' of 'a_codes' (datetime) holds an invalid date (zero date), "
        "for example '0000-00-00 00:00:00'."
    )
    # Values that are not dates are still stored as NULL under "fail".
    assert stop.batch([col("n", T.INTEGER)], [("x",)]).column("n").to_pylist() == [None]

    text = Conversion(invalid_dates="text")
    batch = text.batch(columns, rows)
    assert batch.schema.field("end_date").type == pa.string()
    assert batch.column("end_date").to_pylist() == ["2020-01-01 00:00:00", "0000-00-00 00:00:00"]
    report = text.report()
    assert report["textDates"] == ["end_date"] and report["emptiedValues"] == 0


def test_type_suggestions_for_text_columns() -> None:
    iso = [f"2020-01-0{d}" for d in range(1, 8)]
    assert suggest(iso) == {"type": "date", "format": None, "checked": 7}
    assert suggest([*iso, "0000-00-00"]) == {
        "type": "date",
        "format": None,
        "checked": 8,
        "invalidDates": 1,
    }
    times = [f"2020-01-0{d} 10:00:00" for d in range(1, 7)]
    assert suggest(times) == {"type": "datetime", "format": None, "checked": 6}
    day_first = ["13/02/2020", "01/03/2020", "02/04/2021", "05/06/2022", "07/08/2023"]
    assert suggest(day_first) == {"type": "date", "format": "%d/%m/%Y", "checked": 5}
    ambiguous = suggest(["01/02/2020", "02/03/2020", "03/04/2021", "05/06/2022", "07/08/2023"])
    assert ambiguous is not None and ambiguous["format"] == "%d/%m/%Y" and "note" in ambiguous
    assert suggest(["1 Jan 2020", "2 Feb 2020", "3 Mar 2021", "4 Apr 2022", "5 May 2023"]) == {
        "type": "date",
        "format": "%d %b %Y",
        "checked": 5,
    }
    assert suggest(["12", "7", "n/a", "1,234", "30", "-4"]) == {
        "type": "integer",
        "format": None,
        "checked": 6,
        "emptyWords": 1,
    }
    assert suggest(["1.5", "2", "3.25", "4", "5e2"]) == {
        "type": "decimal",
        "format": None,
        "checked": 5,
    }
    assert suggest(["yes", "no", "Y", "N", "yes"]) == {
        "type": "boolean",
        "format": None,
        "checked": 5,
    }
    assert suggest(["0812", "3000", "3168", "2000", "0800"]) is None  # postcodes stay text
    assert suggest(["12", "7", "abc", "30", "4"]) is None  # not consistent
    assert suggest(["2020-01-01", "2020-01-02"]) is None  # too few to tell
    assert suggest(["0000-00-00"] * 9 + ["2020-01-01"]) is None  # hardly any real dates


def test_overrides_convert_after_the_connector_and_count_what_does_not_fit() -> None:
    conversion = Conversion(table="t")
    columns = [
        col("when", T.STRING),
        col("amount", T.STRING),
        col("n", T.INTEGER),
        col("flag", T.STRING),
        col("left_alone", T.STRING),
    ]
    rows = [
        ("31/12/2026", "1,234.5", 1, "yes", "a"),
        ("00/00/0000", "n/a", 2, "nope", "b"),
        ("not a date", "7", None, "0", "c"),
    ]
    overrides = {
        "when": {"type": "date", "format": "%d/%m/%Y"},
        "amount": {"type": "decimal"},
        "n": {"type": "text"},
        "flag": {"type": "boolean"},
        "missing": {"type": "integer"},  # a column the source no longer has: ignored
        "left_alone": {"type": "unknown"},  # not a type: ignored
    }
    batches = list(conversion.shape(iter([conversion.batch(columns, rows)]), overrides))
    batch = batches[0]
    assert batch.schema.field("when").type == pa.date32()
    assert batch.column("when").to_pylist() == [date(2026, 12, 31), None, None]
    assert batch.column("amount").to_pylist() == [1234.5, None, 7.0]
    assert batch.column("n").to_pylist() == ["1", "2", None]
    assert batch.column("flag").to_pylist() == [True, None, False]
    assert batch.column("left_alone").to_pylist() == ["a", "b", "c"]
    report = conversion.report()
    found = emptied(report)
    assert reasons(found["when"]) == {"zero date": 1, "not a date like %d/%m/%Y": 1}
    assert reasons(found["amount"]) == {"not a number": 1}
    assert reasons(found["flag"]) == {"not true or false": 1}
    assert set(report["overrides"]) == {"when", "amount", "n", "flag", "missing"}


def test_date_formats_are_checked() -> None:
    assert check_format(" %d/%m/%Y ") == "%d/%m/%Y"
    for bad, message in (
        ("dd/mm/yyyy", "% codes"),
        ("%d/%m", "needs a year"),
        ("%Q/%Y", "cannot be used"),
    ):
        with pytest.raises(ValueError, match=message):
            check_format(bad)


def test_mysql_column_types_for_legacy_schemas() -> None:
    def kind(data_type: str, column_type: str, precision: Any = None, scale: Any = None) -> Any:
        spec = column_spec("c", data_type, column_type, True, precision, scale)
        return spec.type.value, spec.scale

    assert kind("tinyint", "tinyint(1)") == ("boolean", None)
    assert kind("bit", "bit(1)") == ("boolean", None)
    assert kind("bit", "bit(8)") == ("integer", None)
    assert kind("bit", "bit(64)") == ("decimal", 0)
    assert kind("bigint", "bigint unsigned") == ("decimal", 0)
    assert kind("bigint", "bigint") == ("integer", None)
    assert kind("int", "int unsigned") == ("integer", None)
    assert kind("decimal", "decimal(10,2)", 10, 2) == ("decimal", 2)
    assert kind("decimal", "decimal(65,30)", 65, 30) == ("decimal", None)  # stored as a double
    assert kind("year", "year") == ("integer", None)
    for text_kind in ("enum", "set", "time", "varchar", "text"):
        assert kind(text_kind, f"{text_kind}('a')") == ("string", None)


# ---- MySQL: a legacy MyISAM table in a non-strict sql_mode ------------------------------------

LEGACY_TABLE = """
CREATE TABLE opsis_legacy.a_codes (
  id INT NOT NULL PRIMARY KEY,
  code VARCHAR(20),
  start_date DATE,
  end_date DATETIME,
  date_created DATETIME,
  date_updated TIMESTAMP NULL DEFAULT NULL,
  kind ENUM('new', 'old'),
  tags SET('x', 'y', 'z'),
  yr YEAR,
  flag BIT(1),
  bits BIT(8),
  wide BIT(64),
  active TINYINT(1),
  price DECIMAL(10,2),
  huge DECIMAL(65,30),
  counter BIGINT UNSIGNED,
  meta JSON,
  slot TIME,
  amount_text VARCHAR(20),
  when_text VARCHAR(20),
  code_twice VARCHAR(40) AS (CONCAT(code, code)) VIRTUAL,
  price_with_tax DECIMAL(12,2) AS (price * 1.1) STORED
) ENGINE=MyISAM DEFAULT CHARSET=utf8mb3
"""

LEGACY_COLUMNS = (
    "id, code, start_date, end_date, date_created, date_updated, kind, tags, yr, flag, bits, "
    "wide, active, price, huge, counter, meta, slot, amount_text, when_text"
)

# sql_mode '' lets MySQL keep zero dates, zero days and zero months, and turns a
# bad ENUM value into ''. The last row goes in with ALLOW_INVALID_DATES.
LEGACY_ROWS = f"""
INSERT INTO opsis_legacy.a_codes ({LEGACY_COLUMNS}) VALUES
(1, 'A1', '2020-01-01', '2024-01-01 09:00:00', '2019-12-31 08:00:00', '2024-01-02 10:00:00',
 'new', 'x,z', 2024, b'1', b'00000101', b'{"1" * 64}', 1, 10.50,
 12345678901234567890.123456789, 18446744073709551615, '{{"a": 1}}', '-01:30:00', '12',
 '01/02/2020'),
(2, 'B2', '2020-05-00', '0000-00-00 00:00:00', '0000-00-00 00:00:00', NULL,
 'old', '', 0, b'0', b'00000000', b'0', 0, 0.00, 0, 0, NULL, '838:59:59', 'n/a', '13/02/2020'),
(3, 'C3', '2020-00-00', '0000-00-00 00:00:00', '2020-01-01 00:00:00', '0000-00-00 00:00:00',
 'bogus', 'y', 1999, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'abc', '03/04/2021'),
(4, 'D4', '0000-00-00', NULL, '0000-00-00 00:00:00', NULL,
 NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '1,234', '05/06/2022'),
(5, 'E5', NULL, '2024-03-01 12:00:00', NULL, NULL,
 NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '7', '07/08/2023')
"""

LEGACY_INVALID = f"""
INSERT INTO opsis_legacy.a_codes ({LEGACY_COLUMNS}) VALUES
(6, 'F6', '2021-02-30', '2021-02-30 10:00:00', NULL, NULL,
 NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '8', '09/10/2024')
"""


@pytest.fixture
def legacy_mysql() -> Iterator[dict[str, Any]]:
    conn = pymysql.connect(host=MYSQL_HOST, user="root", password="root-sample-pw", autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS opsis_legacy")
        cur.execute("CREATE DATABASE opsis_legacy")
        cur.execute("SET SESSION sql_mode = ''")
        cur.execute(LEGACY_TABLE)
        cur.execute(LEGACY_ROWS)
        cur.execute("SET SESSION sql_mode = 'ALLOW_INVALID_DATES'")
        cur.execute(LEGACY_INVALID)
        cur.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_NAME = 'a_codes'")
        assert cur.fetchone() == ("MyISAM",)
    config = {
        "host": MYSQL_HOST,
        "database": "opsis_legacy",
        "user": "root",
        "password": "root-sample-pw",
    }
    yield {"config": config, "conn": conn}
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS opsis_legacy")
    conn.close()


@needs_mysql
def test_mysql_legacy_table_refreshes_and_reports_its_invalid_values(
    client_for: ClientFor, legacy_mysql: dict[str, Any]
) -> None:
    client = client_for()
    connection = connect(client, "mysql", legacy_mysql["config"])
    streams = {s["name"]: s for s in ok(client.get(f"/api/connections/{connection}/streams"))}
    types = {c["name"]: c["type"] for c in streams["a_codes"]["columns"]}
    assert types == {
        "id": "integer",
        "code": "string",
        "start_date": "date",
        "end_date": "timestamp",
        "date_created": "timestamp",
        "date_updated": "timestamp",
        "kind": "string",
        "tags": "string",
        "yr": "integer",
        "flag": "boolean",
        "bits": "integer",
        "wide": "decimal",
        "active": "boolean",
        "price": "decimal",
        "huge": "decimal",
        "counter": "decimal",
        "meta": "json",
        "slot": "string",
        "amount_text": "string",
        "when_text": "string",
        "code_twice": "string",
        "price_with_tax": "decimal",
    }

    dataset = make_dataset(client, connection, "a_codes", name="a_codes")
    run_jobs(client)
    found = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert found["lastJob"]["status"] == "succeeded", found["lastJob"]
    assert found["snapshot"]["rowCount"] == 6 and found["snapshot"]["emptiedValues"] == 10

    report = found["snapshot"]["report"]
    by_column = emptied(report)
    assert set(by_column) == {"start_date", "end_date", "date_created", "date_updated"}
    assert by_column["end_date"]["count"] == 3
    assert reasons(by_column["end_date"]) == {"zero date": 2, "impossible date": 1}
    assert by_column["end_date"]["samples"] == ["0000-00-00 00:00:00", "2021-02-30 10:00:00"]
    assert by_column["end_date"]["sourceType"] == "datetime"
    assert by_column["date_created"]["count"] == 2
    assert by_column["date_created"]["samples"] == ["0000-00-00 00:00:00"]
    assert reasons(by_column["start_date"]) == {
        "partial date (zero year, month or day)": 2,
        "zero date": 1,
        "impossible date": 1,
    }
    assert by_column["date_updated"]["count"] == 1
    assert report["invalidDates"] == "null"
    assert report["suggestions"] == [
        {"column": "when_text", "type": "date", "format": "%d/%m/%Y", "checked": 6}
    ]
    assert report["sourceTypes"]["counter"] == "bigint unsigned"

    rows = {row["id"]: row for row in rows_of(client, dataset["id"])}
    assert rows[1] == {
        "id": 1,
        "code": "A1",
        "start_date": "2020-01-01",
        "end_date": "2024-01-01T09:00:00",
        "date_created": "2019-12-31T08:00:00",
        "date_updated": "2024-01-02T10:00:00",
        "kind": "new",
        "tags": "x,z",
        "yr": 2024,
        "flag": True,
        "bits": 5,
        "wide": "18446744073709551615",
        "active": True,
        "price": "10.50",
        "huge": pytest.approx(12345678901234567890.123456789),
        "counter": "18446744073709551615",
        "meta": '{"a": 1}',
        "slot": "-01:30:00",
        "amount_text": "12",
        "when_text": "01/02/2020",
        "code_twice": "A1A1",
        "price_with_tax": "11.55",
    }
    assert rows[2]["end_date"] is None and rows[2]["date_created"] is None
    assert rows[2]["start_date"] is None and rows[2]["slot"] == "838:59:59"
    assert rows[2]["flag"] is False and rows[2]["wide"] == "0" and rows[2]["yr"] == 0
    assert rows[3]["kind"] == "" and rows[3]["date_created"] == "2020-01-01T00:00:00"
    assert rows[6]["end_date"] is None and rows[6]["start_date"] is None

    # The refresh job's result carries the same report.
    [job] = ok(client.get(f"/api/jobs?datasetId={dataset['id']}"))
    quality = job["result"]["dataQuality"]
    assert quality["emptiedValues"] == 10 and emptied(quality)["end_date"]["count"] == 3
    # A list of datasets shows the count only.
    [listed] = ok(client.get("/api/datasets"))
    assert listed["lastJob"]["result"]["dataQuality"] == {"emptiedValues": 10}
    assert "report" not in listed["snapshot"]

    # And so does MCP.
    described = Mcp(client).call("describe_dataset", dataset="a_codes")
    quality = described["dataQuality"]
    assert quality["emptiedValues"] == 10
    assert emptied({"emptied": quality["emptiedByColumn"]})["date_created"]["count"] == 2
    assert quality["suggestedTypes"][0]["column"] == "when_text"
    status = Mcp(client).call("get_job_status", job_id=job["id"])
    assert status["result"]["dataQuality"]["emptiedValues"] == 10


@needs_mysql
def test_column_types_set_by_hand_rebuild_the_dataset_and_survive_source_changes(
    client_for: ClientFor, legacy_mysql: dict[str, Any], db: Session
) -> None:
    client = client_for()
    connection = connect(client, "mysql", legacy_mysql["config"])
    dataset = make_dataset(client, connection, "a_codes", name="a_codes")
    path = f"/api/datasets/{dataset['id']}/columns"

    early = client.put(path, json={"overrides": {"when_text": {"type": "date"}}})
    assert early.status_code == 409 and "Refresh the dataset first" in early.text
    run_jobs(client)

    bad = client.put(
        path,
        json={
            "overrides": {
                "nope": {"type": "date"},
                "code": {"type": "colour"},
                "amount_text": {"type": "integer", "format": "%d"},
                "when_text": {"type": "date", "format": "dd/mm/yyyy"},
            }
        },
    )
    assert bad.status_code == 422, bad.text
    problems = {p["path"]: p["message"] for p in bad.json()["error"]["details"]}
    assert set(problems) == {
        "/overrides/nope",
        "/overrides/code",
        "/overrides/amount_text",
        "/overrides/when_text",
    }
    assert "no column 'nope'" in problems["/overrides/nope"]

    saved = ok(
        client.put(
            path,
            json={
                "overrides": {
                    "when_text": {"type": "date", "format": "%d/%m/%Y"},
                    "amount_text": {"type": "integer"},
                    "yr": {"type": "text"},
                    "code": None,
                }
            },
        )
    )
    assert saved["columnOverrides"] == {
        "when_text": {"type": "date", "format": "%d/%m/%Y"},
        "amount_text": {"type": "integer"},
        "yr": {"type": "text"},
    }
    assert saved["job"]["status"] == "queued" and saved["job"]["full"] is True
    run_jobs(client)

    found = ok(client.get(f"/api/datasets/{dataset['id']}"))
    types = {c["name"]: c["type"] for c in found["snapshot"]["columns"]}
    assert types["when_text"] == "date" and types["amount_text"] == "integer"
    assert types["yr"] == "string"
    rows = {row["id"]: row for row in rows_of(client, dataset["id"])}
    assert [rows[i]["when_text"] for i in (1, 2)] == ["2020-02-01", "2020-02-13"]
    assert [rows[i]["amount_text"] for i in range(1, 7)] == [12, None, None, 1234, 7, 8]
    assert rows[1]["yr"] == "2024"
    report = found["snapshot"]["report"]
    amount = emptied(report)["amount_text"]
    assert amount["count"] == 2 and amount["samples"] == ["n/a", "abc"]
    assert amount["sourceType"] == "varchar(20)"
    assert report["suggestions"] == []  # when_text now is a date
    changes = db.scalars(
        select(AuditLog).where(AuditLog.action == "dataset.column_types_changed")
    ).all()
    assert len(changes) == 1 and changes[0].detail["after"]["amount_text"] == {"type": "integer"}

    # Saving the same choices again changes nothing and queues nothing.
    again = ok(client.put(path, json={"overrides": saved["columnOverrides"]}))
    assert again["job"] is None

    # The source changes: a new column. The choices are kept.
    with legacy_mysql["conn"].cursor() as cur:
        cur.execute("ALTER TABLE opsis_legacy.a_codes ADD COLUMN extra VARCHAR(5) DEFAULT 'e'")
    after = refresh(client, dataset["id"])
    types = {c["name"]: c["type"] for c in after["snapshot"]["columns"]}
    assert types["extra"] == "string" and types["when_text"] == "date"
    assert after["columnOverrides"]["amount_text"] == {"type": "integer"}


@needs_mysql
def test_invalid_dates_setting_stop_or_text(
    client_for: ClientFor, legacy_mysql: dict[str, Any]
) -> None:
    client = client_for()
    config = {**legacy_mysql["config"], "invalid_dates": "fail"}
    connection = connect(client, "mysql", config)
    dataset = make_dataset(client, connection, "a_codes", name="a_codes")
    run_jobs(client)
    failed = ok(client.get(f"/api/datasets/{dataset['id']}"))["lastJob"]
    assert failed["status"] == "failed"
    assert failed["error"].startswith(
        "Column 'start_date' of 'a_codes' (date) holds an invalid date (partial date (zero "
        "year, month or day)), for example '2020-05-00'."
    )

    config["invalid_dates"] = "text"
    ok(client.patch(f"/api/connections/{connection}", json={"config": config}))
    found = refresh(client, dataset["id"], full=True)
    assert found["lastJob"]["status"] == "succeeded", found["lastJob"]
    types = {c["name"]: c["type"] for c in found["snapshot"]["columns"]}
    assert {types[n] for n in ("start_date", "end_date", "date_created", "date_updated")} == {
        "string"
    }
    rows = {row["id"]: row for row in rows_of(client, dataset["id"])}
    assert rows[1]["end_date"] == "2024-01-01 09:00:00"
    assert rows[2]["end_date"] == "0000-00-00 00:00:00" and rows[2]["start_date"] == "2020-05-00"
    assert rows[6]["start_date"] == "2021-02-30"
    report = found["snapshot"]["report"]
    assert report["invalidDates"] == "text" and report["emptiedValues"] == 0
    assert report["textDates"] == ["start_date", "end_date", "date_created", "date_updated"]
    assert [s["column"] for s in report["suggestions"]] == ["when_text"]

    bad = client.patch(
        f"/api/connections/{connection}", json={"config": {**config, "invalid_dates": "maybe"}}
    )
    assert bad.status_code == 422 and "/config/invalid_dates" in bad.text


@needs_mysql
def test_append_refresh_over_a_cursor_column_with_zero_dates(
    client_for: ClientFor, legacy_mysql: dict[str, Any]
) -> None:
    client = client_for()
    connection = connect(client, "mysql", legacy_mysql["config"])
    dataset = make_dataset(
        client, connection, "a_codes", name="a_codes", refreshMode="append", cursorField="end_date"
    )
    run_jobs(client)
    first = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert first["lastJob"]["status"] == "succeeded", first["lastJob"]
    assert first["snapshot"]["rowCount"] == 6
    with legacy_mysql["conn"].cursor() as cur:
        cur.execute("SET SESSION sql_mode = ''")
        cur.execute(
            "INSERT INTO opsis_legacy.a_codes (id, code, end_date) VALUES "
            "(7, 'G7', '2025-01-01 00:00:00'), (8, 'H8', '0000-00-00 00:00:00')"
        )
    second = refresh(client, dataset["id"])
    assert second["lastJob"]["status"] == "succeeded", second["lastJob"]
    # Only the row after the newest real end_date; the zero date is not "after" anything.
    assert second["snapshot"]["rowCount"] == 7


# ---- PostgreSQL ------------------------------------------------------------------------------


@pytest.fixture
def pg_legacy(migrated_db: Settings) -> Iterator[dict[str, Any]]:
    config = {
        "host": migrated_db.postgres_host,
        "port": migrated_db.postgres_port,
        "database": migrated_db.postgres_db,
        "user": migrated_db.postgres_user,
        "password": migrated_db.postgres_password.get_secret_value(),
    }
    conninfo = {k if k != "database" else "dbname": v for k, v in config.items()}
    with psycopg.connect(**conninfo, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS legacy CASCADE")
        conn.execute("CREATE SCHEMA legacy")
        conn.execute(
            """CREATE TABLE legacy.events (
                 id integer PRIMARY KEY, happened date, at_time timestamp, at_instant timestamptz,
                 span interval, big numeric(50, 10))"""
        )
        conn.execute(
            """INSERT INTO legacy.events VALUES
               (1, '2026-01-05', '2026-01-05 10:00', '2026-01-05 10:00+00',
                '1 year 2 mons 3 days 04:05:06', 12345678901234567890123456789.5),
               (2, 'infinity', '-infinity', 'infinity', '-2 hours', NULL),
               (3, '0044-03-15 BC', '10000-01-01 00:00', NULL, NULL, NULL)"""
        )
        yield {"config": config}
        conn.execute("DROP SCHEMA IF EXISTS legacy CASCADE")


def test_postgres_dates_python_cannot_hold_are_reported(
    client_for: ClientFor, pg_legacy: dict[str, Any]
) -> None:
    client = client_for()
    connection = connect(client, "postgres", pg_legacy["config"])
    dataset = make_dataset(client, connection, "legacy.events", name="events")
    run_jobs(client)
    found = ok(client.get(f"/api/datasets/{dataset['id']}"))
    assert found["lastJob"]["status"] == "succeeded", found["lastJob"]
    types = {c["name"]: c["type"] for c in found["snapshot"]["columns"]}
    assert types["span"] == "string" and types["big"] == "float"
    by_column = emptied(found["snapshot"]["report"])
    assert reasons(by_column["happened"]) == {"infinite date": 1, "date before year 1": 1}
    assert reasons(by_column["at_time"]) == {"infinite date": 1, "year after 9999": 1}
    assert by_column["at_time"]["samples"] == ["-infinity", "10000-01-01 00:00:00"]
    assert reasons(by_column["at_instant"]) == {"infinite date": 1}
    rows = {row["id"]: row for row in rows_of(client, dataset["id"])}
    assert rows[1]["happened"] == "2026-01-05"
    assert rows[1]["span"] == "1 year 2 mons 3 days 04:05:06"  # as Postgres writes it
    assert rows[2]["span"] == "-02:00:00"
    assert rows[1]["big"] == pytest.approx(1.2345678901234568e28)
    assert rows[2]["happened"] is None and rows[3]["at_time"] is None


# ---- files ---------------------------------------------------------------------------------

CSV = (
    "id,amount,when_mon,zero,flag\n"
    "1,12,1 Jan 2020,2020-01-01,yes\n"
    "2,n/a,2 Feb 2020,0000-00-00,no\n"
    "3,7,3 Mar 2021,2020-01-03,yes\n"
    "4,15,4 Apr 2022,2020-01-04,no\n"
    "5,20,5 May 2023,2020-01-05,yes\n"
    "6,30,6 Jun 2024,2020-01-06,no\n"
)


def test_csv_text_columns_get_suggestions_and_can_be_typed_by_hand(client_for: ClientFor) -> None:
    client = client_for()
    connection = connect(
        client, "file", {"file": upload(client, "sloppy.csv", CSV.encode())["file"]}
    )
    dataset = make_dataset(client, connection, "sloppy", name="sloppy")
    run_jobs(client)
    found = ok(client.get(f"/api/datasets/{dataset['id']}"))
    types = {c["name"]: c["type"] for c in found["snapshot"]["columns"]}
    assert types == {
        "id": "integer",
        "amount": "string",
        "when_mon": "string",
        "zero": "string",
        "flag": "boolean",
    }
    suggestions = {s["column"]: s for s in found["snapshot"]["report"]["suggestions"]}
    assert suggestions == {
        "amount": {
            "column": "amount",
            "type": "integer",
            "format": None,
            "checked": 6,
            "emptyWords": 1,
        },
        "when_mon": {"column": "when_mon", "type": "date", "format": "%d %b %Y", "checked": 6},
        "zero": {
            "column": "zero",
            "type": "date",
            "format": None,
            "checked": 6,
            "invalidDates": 1,
        },
    }
    overrides = {
        name: {"type": s["type"], "format": s["format"]} for name, s in suggestions.items()
    }
    ok(client.put(f"/api/datasets/{dataset['id']}/columns", json={"overrides": overrides}))
    run_jobs(client)
    found = ok(client.get(f"/api/datasets/{dataset['id']}"))
    rows = rows_of(client, dataset["id"])
    assert [r["amount"] for r in rows] == [12, None, 7, 15, 20, 30]
    assert rows[0]["when_mon"] == "2020-01-01" and rows[1]["zero"] is None
    by_column = emptied(found["snapshot"]["report"])
    assert reasons(by_column["amount"]) == {"not a number": 1}
    assert reasons(by_column["zero"]) == {"zero date": 1}


def test_excel_mixed_columns_are_text_with_a_suggestion(client_for: ClientFor) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Gifts"
    sheet.append(["Fund", "Amount"])
    for fund, amount in (("A", 10.5), ("B", "n/a"), ("C", 20), ("D", 7.25), ("E", 3), ("F", 1)):
        sheet.append([fund, amount])
    buffer = io.BytesIO()
    book.save(buffer)
    client = client_for()
    file = upload(client, "gifts.xlsx", buffer.getvalue())["file"]
    connection = connect(client, "file", {"file": file})
    dataset = make_dataset(client, connection, "Gifts", name="gifts")
    run_jobs(client)
    found = ok(client.get(f"/api/datasets/{dataset['id']}"))
    [suggestion] = found["snapshot"]["report"]["suggestions"]
    assert suggestion["column"] == "Amount" and suggestion["type"] == "decimal"
    ok(
        client.put(
            f"/api/datasets/{dataset['id']}/columns",
            json={"overrides": {"Amount": {"type": "decimal"}}},
        )
    )
    run_jobs(client)
    assert [r["Amount"] for r in rows_of(client, dataset["id"])] == [
        10.5,
        None,
        20.0,
        7.25,
        3.0,
        1.0,
    ]


# ---- Google Sheets and Salesforce ----------------------------------------------------------


def test_sheets_cells_that_do_not_fit_their_column_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_gsheets as sheets

    values = [list(row) for row in sheets.VALUES]
    values[4][3] = "not yet"  # text in the gift_date column (a date column)
    values[4][4] = "maybe"  # text in the paid column (true or false)
    monkeypatch.setattr(sheets, "VALUES", values)
    conversion = Conversion(table="Gifts")
    result = sheets.connector(sheets.FakeSheets()).read(
        "Gifts", RefreshMode.FULL, None, ReadOptions(conversion=conversion)
    )
    table = next(iter(result.batches)).to_pydict()
    assert table["gift_date"] == [date(2026, 1, 15), date(2026, 1, 16), None, None]
    assert table["paid"] == [True, False, None, None]
    by_column = emptied(conversion.report())
    assert by_column["gift_date"]["samples"] == ["not yet"]
    assert reasons(by_column["gift_date"]) == {"not a date": 1}
    assert reasons(by_column["paid"]) == {"not true or false": 1}


def test_salesforce_values_that_do_not_parse_are_counted() -> None:
    from tests import test_salesforce as sf

    fake = sf.FakeSalesforce()
    fake.accounts[1]["CreatedDate"] = "not a date"
    fake.accounts[2]["AnnualRevenue"] = "lots"
    conversion = Conversion(table="Account")
    result = sf.connector(fake).read(
        "Account", RefreshMode.FULL, None, ReadOptions(conversion=conversion)
    )
    table = next(iter(result.batches)).to_pydict()
    assert table["CreatedDate"] == [
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        datetime(2026, 1, 1, tzinfo=UTC),
    ]
    assert table["AnnualRevenue"] == [Decimal("1500000"), Decimal("1500000"), None]
    by_column = emptied(conversion.report())
    assert by_column["CreatedDate"]["samples"] == ["not a date"]
    assert reasons(by_column["AnnualRevenue"]) == {"not a number": 1}


# ---- who sees the sample values ------------------------------------------------------------


def test_sample_values_are_hidden_from_people_who_may_not_see_the_dataset(
    client_for: ClientFor, db: Session
) -> None:
    client = client_for()
    csv = "id,when\n1,0000-00-00\n2,2020-01-01\n"
    connection = connect(client, "file", {"file": upload(client, "w.csv", csv.encode())["file"]})
    dataset = make_dataset(client, connection, "w", name="w")
    run_jobs(client)
    ok(
        client.put(
            f"/api/datasets/{dataset['id']}/columns",
            json={"overrides": {"when": {"type": "date"}}},
        )
    )
    run_jobs(client)
    record = db.get(Dataset, uuid.UUID(dataset["id"]))
    assert record is not None
    record.restricted = True
    db.commit()

    settings = settings_of(client)
    editor = make_user(db, "editor")
    admin = make_user(db, "admin")
    assert datasets.samples_hidden_from(db, settings, editor, {dataset["id"]}) == {dataset["id"]}
    assert datasets.samples_hidden_from(db, settings, admin, set()) == set()
    assert datasets.samples_hidden_from(db, settings, admin, {dataset["id"]}) == set()

    jobs = datasets.list_jobs(db, None, 10, settings=settings, principal=editor)
    reported = [j for j in jobs if (j["result"] or {}).get("dataQuality", {}).get("emptied")]
    assert reported, jobs
    for job in reported:
        for entry in job["result"]["dataQuality"]["emptied"]:
            assert entry["samples"] == [] and entry["samplesHidden"] is True
    for job in datasets.list_jobs(db, None, 10, settings=settings, principal=admin):
        for entry in (job["result"] or {}).get("dataQuality", {}).get("emptied", []):
            assert entry["samples"] == ["0000-00-00"]
    one = db.scalars(
        select(Job)
        .where(Job.type == datasets.REFRESH_JOB, Job.status == "succeeded")
        .order_by(Job.created_at.desc())
    ).first()
    assert one is not None
    hidden = datasets.get_job(db, one.id, settings=settings, principal=editor)
    assert hidden["result"]["dataQuality"]["emptied"][0]["samplesHidden"] is True


def test_every_connection_has_the_invalid_dates_setting(client_for: ClientFor) -> None:
    for connector in ok(client_for().get("/api/connectors")):
        field = connector["configSchema"]["properties"]["invalid_dates"]
        assert field["enum"] == ["null", "text", "fail"] and field["default"] == "null"
        assert set(field["x-enum-labels"]) == {"null", "text", "fail"}
