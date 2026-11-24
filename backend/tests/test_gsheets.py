"""The Google Sheets connector, against a fake Sheets API that answers as the real
one does (values unformatted, dates as serial numbers, formats from grid data)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from urllib.parse import unquote

import httpx2
import pytest
from fastapi.testclient import TestClient

from opsis.connectors import gsheets
from opsis.connectors.base import ColumnType, ConnectorError, RefreshMode
from opsis.connectors.gsheets import GoogleSheetsConnector, spreadsheet_id
from tests.test_ingestion import connect, make_dataset, ok, run_jobs

ClientFor = Callable[..., TestClient]
SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-xyz"
KEY = json.dumps(
    {
        "type": "service_account",
        "client_email": "opsis-reader@demo-project.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
    }
)


def number(value: float, kind: str | None = None) -> dict[str, Any]:
    cell: dict[str, Any] = {"effectiveValue": {"numberValue": value}}
    if kind:
        cell["effectiveFormat"] = {"numberFormat": {"type": kind}}
    return cell


def text(value: str) -> dict[str, Any]:
    return {"effectiveValue": {"stringValue": value}}


def flag(value: bool) -> dict[str, Any]:
    return {"effectiveValue": {"boolValue": value}}


# 15 Jan 2026 is serial 46037; 46037.5 is noon that day; 0.375 is 09:00.
HEADER = ["id", "fund", "amount", "gift_date", "paid", "received_at", "note", "slot"]
GRID = [
    [text(h) for h in HEADER],
    [number(1), text("Library"), number(100), number(46037, "DATE"), flag(True),
     number(46037.5, "DATE_TIME"), text("first"), number(0.375, "TIME")],
    [number(2), text("Sport"), number(250.5), number(46038, "DATE"), flag(False),
     number(46038.25, "DATE_TIME"), number(7), number(0.5, "TIME")],
]  # fmt: skip
VALUES = [
    HEADER,
    [1, "Library", 100, 46037, True, 46037.5, "first", 0.375],
    [2, "Sport", 250.5, 46038, False, 46038.25, 7, 0.5],
    [],  # a blank row in the middle is skipped
    [3, "Research", 75, 46040, True, 46040, "", 0.75],
    [4.5, "Library", 10, "", "", "", "late text", ""],  # a fraction below the sample
]


class FakeSheets:
    """Answers the three requests the connector makes, and records them."""

    def __init__(self, rows: int = 6, error: tuple[int, str] | None = None) -> None:
        self.rows = rows
        self.error = error
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if self.error:
            status, message = self.error
            return httpx2.Response(status, json={"error": {"code": status, "message": message}})
        path = unquote(request.url.path)
        params = dict(request.url.params)
        if path == f"/v4/spreadsheets/{SHEET}" and "includeGridData" in params:
            assert params["ranges"] == "'Gifts'!1:201"
            return httpx2.Response(
                200, json={"sheets": [{"data": [{"rowData": [{"values": r} for r in GRID]}]}]}
            )
        if path == f"/v4/spreadsheets/{SHEET}":
            sheets = [
                {"properties": {"sheetId": 0, "title": "Gifts", "sheetType": "GRID",
                                "gridProperties": {"rowCount": self.rows, "columnCount": 8}}},
                {"properties": {"sheetId": 1, "title": "A chart", "sheetType": "OBJECT"}},
            ]  # fmt: skip
            return httpx2.Response(200, json={"properties": {"title": "Giving"}, "sheets": sheets})
        found = re.fullmatch(rf"/v4/spreadsheets/{SHEET}/values/'Gifts'!(\d+):(\d+)", path)
        if found:
            assert params["valueRenderOption"] == "UNFORMATTED_VALUE"
            assert params["dateTimeRenderOption"] == "SERIAL_NUMBER"
            start = int(found.group(1))
            page = VALUES[start - 1 : int(found.group(2))]
            while page and not page[-1]:
                page = page[:-1]  # Sheets leaves trailing empty rows out
            return httpx2.Response(200, json={"values": page})
        raise AssertionError(f"unexpected request {request.url}")


def connector(fake: FakeSheets, **config: Any) -> GoogleSheetsConnector:
    return GoogleSheetsConnector(
        {"spreadsheet": f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=0",
         "service_account_key": KEY, **config},
        transport=httpx2.MockTransport(fake.handler),
        token=lambda: "test-token",
    )  # fmt: skip


def test_tabs_become_streams_with_types_from_the_cells() -> None:
    fake = FakeSheets()
    result = connector(fake).test_connection()
    assert result.ok and result.message == (
        "Found 'Giving' with 1 tab(s), reading as opsis-reader@demo-project.iam.gserviceaccount.com."
    )
    [stream] = connector(fake).discover()  # the chart tab is not a table
    assert stream.name == "Gifts" and stream.row_count_estimate == 5
    assert stream.supported_modes == (RefreshMode.FULL,)
    types = {c.name: c.type for c in stream.columns}
    assert types == {
        "id": ColumnType.INTEGER,
        "fund": ColumnType.STRING,
        "amount": ColumnType.FLOAT,
        "gift_date": ColumnType.DATE,
        "paid": ColumnType.BOOLEAN,
        "received_at": ColumnType.TIMESTAMP,
        "note": ColumnType.STRING,  # text and a number
        "slot": ColumnType.STRING,  # times of day
    }


def test_reading_converts_values_and_trusts_every_row_over_the_sample() -> None:
    batches = list(connector(FakeSheets()).read("Gifts", RefreshMode.FULL, None).batches)
    table = batches[0].to_pydict()
    # A fraction below the sample makes id a decimal column.
    assert batches[0].schema.field("id").type == "double"
    assert table["id"] == [1.0, 2.0, 3.0, 4.5]
    assert table["gift_date"] == [date(2026, 1, 15), date(2026, 1, 16), date(2026, 1, 18), None]
    assert table["received_at"] == [
        datetime(2026, 1, 15, 12, 0),
        datetime(2026, 1, 16, 6, 0),
        datetime(2026, 1, 18, 0, 0),
        None,
    ]
    assert table["paid"] == [True, False, True, None]
    assert table["note"] == ["first", "7", None, "late text"]
    assert table["slot"] == ["09:00:00", "12:00:00", "18:00:00", None]
    only = list(connector(FakeSheets()).read("Gifts", RefreshMode.FULL, None).batches)
    assert only[0].num_rows == 4


def test_large_tabs_are_read_a_page_at_a_time() -> None:
    fake = FakeSheets(rows=25_000)
    list(connector(fake).read("Gifts", RefreshMode.FULL, None).batches)
    pages = [
        unquote(r.url.path).rsplit("!", 1)[1] for r in fake.requests if "/values/" in r.url.path
    ]
    assert pages == ["1:10000", "10001:20000", "20001:30000"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ((403, "The caller does not have permission"),
         "not shared with opsis-reader@demo-project.iam.gserviceaccount.com. Share it with that address as a Viewer"),
        ((403, "Google Sheets API has not been used in project 123 before or it is disabled."),
         "The Google Sheets API is not enabled"),
        ((404, "Requested entity was not found."), "There is no spreadsheet with this ID"),
        ((429, "Quota exceeded"), "limiting requests"),
    ],
)  # fmt: skip
def test_errors_say_what_to_do(error: tuple[int, str], expected: str) -> None:
    result = connector(FakeSheets(error=error)).test_connection()
    assert not result.ok and expected in result.message


def test_bad_input_is_explained_before_calling_google() -> None:
    assert spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit") == SHEET
    assert spreadsheet_id(SHEET) == SHEET and spreadsheet_id("not a sheet") is None
    fake = FakeSheets()
    bad_key = connector(fake, service_account_key='{"type": "authorized_user"}').test_connection()
    assert not bad_key.ok and "not a JSON key file" in bad_key.message
    wrong = connector(fake, spreadsheet="https://example.com/sheet").test_connection()
    assert not wrong.ok and "not a spreadsheet URL or ID" in wrong.message
    assert fake.requests == []
    with pytest.raises(ConnectorError, match="no tab called 'Missing'"):
        connector(fake).read("Missing", RefreshMode.FULL, None)
    schema = GoogleSheetsConnector.config_schema()["properties"]["service_account_key"]
    assert schema["x-secret"] is True


def test_a_sheet_becomes_a_dataset_through_the_app(
    client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSheets()
    monkeypatch.setattr(gsheets, "DEFAULT_TRANSPORT", httpx2.MockTransport(fake.handler))
    monkeypatch.setattr(gsheets, "DEFAULT_TOKEN", lambda: "test-token")
    client = client_for()
    connection = connect(
        client,
        "gsheets",
        {"auth": "service_account", "spreadsheet": SHEET, "service_account_key": KEY},
    )
    described = ok(client.get(f"/api/connections/{connection}"))
    assert "service_account_key" not in json.dumps(described.get("config", {}))  # never sent back
    streams = ok(client.get(f"/api/connections/{connection}/streams"))
    assert [s["name"] for s in streams] == ["Gifts"]
    dataset = make_dataset(client, connection, "Gifts", name="sheet_gifts")
    run_jobs(client)
    rows = ok(client.get(f"/api/datasets/{dataset['id']}/preview"))
    assert rows["rowCount"] == 4
