"""The Salesforce connector, against a fake Salesforce behind the requests session
simple-salesforce uses: both sign-in flows, describes, paged SOQL queries and the
incremental merge on SystemModstamp."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from opsis.connectors import salesforce
from opsis.connectors.base import ColumnType, ConnectorError, ReadOptions, RefreshMode
from opsis.connectors.salesforce import SalesforceConnector
from tests.test_ingestion import connect, make_dataset, ok, refresh, rows_of, run_jobs

ClientFor = Callable[..., TestClient]
HOST = "acme-dev-ed.develop.my.salesforce.com"
CLIENT = {
    "my_domain": f"https://{HOST}",
    "auth": "client_credentials",
    "client_id": "3MVG9-test-consumer-key",
    "client_secret": "test-consumer-secret",
}


def field(name: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": kind, "nillable": True, **extra}


FIELDS = {
    "Account": [
        field("Id", "id", nillable=False),
        field("Name", "string"),
        field("Industry", "picklist"),
        field("AnnualRevenue", "currency", scale=0),
        field("NumberOfEmployees", "int"),
        field("IsDeleted", "boolean"),
        field("BillingAddress", "address"),  # compound: its parts are fields of their own
        field("BillingCity", "string"),
        field("CreatedDate", "datetime"),
        field("SystemModstamp", "datetime"),
    ],
    "Opportunity": [
        field("Id", "id"),
        field("Amount", "currency", scale=2),
        field("CloseDate", "date"),
        field("Probability", "percent"),
        field("SystemModstamp", "datetime"),
    ],
    "Donation__c": [
        field("Id", "id"),
        field("Amount__c", "currency", scale=2),
        field("SystemModstamp", "datetime"),
    ],
}


def account(n: int, name: str, stamp: str) -> dict[str, Any]:
    return {
        "attributes": {"type": "Account"},
        "Id": f"001A0000000000{n}AAA",
        "Name": name,
        "Industry": "Education",
        "AnnualRevenue": 1500000,
        "NumberOfEmployees": 42,
        "IsDeleted": False,
        "BillingCity": "Melbourne",
        "CreatedDate": "2026-01-01T00:00:00.000+0000",
        "SystemModstamp": stamp,
    }


class FakeSalesforce(requests.adapters.BaseAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[requests.PreparedRequest] = []
        self.queries: list[str] = []
        self.accounts = [
            account(1, "Alpha", "2026-01-15T10:20:30.500+0000"),
            account(2, "Beta", "2026-01-16T08:00:00.000+0000"),
            account(3, "Gamma", "2026-01-17T09:30:00.250+0000"),
        ]
        self.token_error: dict[str, str] | None = None
        self.query_error: list[dict[str, str]] | None = None

    def respond(
        self,
        request: requests.PreparedRequest,
        status: int,
        body: Any,
        kind: str = "application/json",
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        response.headers["Content-Type"] = kind
        response.url = str(request.url)
        response.request = request
        return response

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        self.requests.append(request)
        assert kwargs.get("timeout") == salesforce.REQUEST_TIMEOUT  # never without a timeout
        url = urlsplit(str(request.url))
        assert url.hostname == HOST
        path = url.path
        if path == "/services/oauth2/token":
            if self.token_error:
                return self.respond(request, 400, self.token_error)
            return self.respond(
                request,
                200,
                {
                    "access_token": "00Dtoken",
                    "instance_url": f"https://{HOST}",
                    "token_type": "Bearer",
                },
            )
        if path.startswith("/services/Soap/u/"):
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
                'xmlns="urn:partner.soap.sforce.com"><soapenv:Body><loginResponse><result>'
                f"<serverUrl>https://{HOST}/services/Soap/u/59.0/00D000000000001</serverUrl>"
                "<sessionId>00Dsession</sessionId></result></loginResponse></soapenv:Body></soapenv:Envelope>"
            )
            return self.respond(request, 200, xml, "text/xml")
        assert request.headers["Authorization"] in ("Bearer 00Dtoken", "Bearer 00Dsession")
        if path.endswith("/sobjects"):
            objects = [
                {"name": n, "queryable": True, "retrieveable": True}
                for n in ("Account", "Opportunity", "Donation__c")
            ]
            objects.append({"name": "ApexLog", "queryable": False})
            return self.respond(request, 200, {"sobjects": objects})
        described = re.search(r"/sobjects/(\w+)/describe$", path)
        if described:
            name = described.group(1)
            return self.respond(
                request, 200, {"name": name, "labelPlural": f"{name}s", "fields": FIELDS[name]}
            )
        if path.endswith("/query/0000-page-2"):
            return self.respond(
                request, 200, {"done": True, "totalSize": 3, "records": self.pending}
            )
        if path.endswith("/query/"):
            soql = parse_qs(url.query)["q"][0]
            self.queries.append(soql)
            if self.query_error:
                return self.respond(request, 400, self.query_error)
            if "FROM Organization" in soql:
                return self.respond(
                    request,
                    200,
                    {
                        "done": True,
                        "records": [
                            {
                                "Name": "Acme",
                                "OrganizationType": "Developer Edition",
                                "IsSandbox": False,
                            }
                        ],
                    },
                )
            records = self.accounts
            found = re.search(r"SystemModstamp >= (\S+)", soql)
            if found:
                since = datetime.fromisoformat(found.group(1).replace("Z", "+00:00"))
                records = [
                    r
                    for r in records
                    if salesforce._value(r["SystemModstamp"], stamp_column()) >= since
                ]
            self.pending = records[2:]
            page: dict[str, Any] = {
                "done": not self.pending,
                "totalSize": len(records),
                "records": records[:2],
            }
            if self.pending:
                page["nextRecordsUrl"] = "/services/data/v59.0/query/0000-page-2"
            return self.respond(request, 200, page)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def close(self) -> None:
        pass


def stamp_column() -> Any:
    return salesforce._column(field("SystemModstamp", "datetime"))


def session_for(fake: FakeSalesforce) -> requests.Session:
    session = salesforce._Session()
    session.mount("https://", fake)
    return session


def connector(fake: FakeSalesforce, **config: Any) -> SalesforceConnector:
    return SalesforceConnector({**CLIENT, **config}, session=session_for(fake))


def test_client_credentials_sign_in_and_the_org_check() -> None:
    fake = FakeSalesforce()
    result = connector(fake).test_connection()
    assert result.ok and result.message == "Connected to Acme, Developer Edition."
    assert result.details == {"organizationType": "Developer Edition", "isSandbox": False}
    token = fake.requests[0]
    assert token.url == f"https://{HOST}/services/oauth2/token"
    assert parse_qs(str(token.body)) == {"grant_type": ["client_credentials"]}
    expected = base64.b64encode(b"3MVG9-test-consumer-key:test-consumer-secret").decode()
    assert token.headers["Authorization"] == f"Basic {expected}"
    # The username flow signs in to the same My Domain.
    fake = FakeSalesforce()
    password = connector(
        fake, auth="password", username="int@acme.test", password="pw-test-1", security_token="tok"
    )
    assert password.test_connection().ok
    assert urlsplit(str(fake.requests[0].url)).path.startswith("/services/Soap/u/")


def test_objects_become_streams_with_salesforce_types() -> None:
    fake = FakeSalesforce()
    streams = {s.name: s for s in connector(fake).discover()}
    assert list(streams) == ["Account", "Opportunity", "Donation__c"]  # ApexLog cannot be queried
    accounts = streams["Account"]
    assert accounts.label == "Accounts" and accounts.primary_key == ("Id",)
    assert accounts.cursor_candidates == ("SystemModstamp", "CreatedDate")
    assert accounts.supported_modes == (RefreshMode.FULL, RefreshMode.MERGE)
    types = {c.name: c.type for c in accounts.columns}
    assert "BillingAddress" not in types and types["BillingCity"] == ColumnType.STRING
    assert (types["AnnualRevenue"], types["NumberOfEmployees"], types["IsDeleted"]) == (
        ColumnType.DECIMAL, ColumnType.INTEGER, ColumnType.BOOLEAN,
    )  # fmt: skip
    created = next(c for c in accounts.columns if c.name == "CreatedDate")
    assert created.type == ColumnType.TIMESTAMP and created.tz_aware
    opportunity = {c.name: c for c in streams["Opportunity"].columns}
    assert opportunity["Amount"].scale == 2 and opportunity["CloseDate"].type == ColumnType.DATE
    # A named list of objects is checked against what the org has.
    only = connector(FakeSalesforce(), objects="Opportunity, Donation__c").discover()
    assert [s.name for s in only] == ["Opportunity", "Donation__c"]
    with pytest.raises(ConnectorError, match="no object it lets this user read called Invoice__c"):
        connector(FakeSalesforce(), objects="Account, Invoice__c").discover()


def test_full_and_incremental_reads() -> None:
    fake = FakeSalesforce()
    full = connector(fake).read("Account", RefreshMode.FULL, None)
    table = [b.to_pydict() for b in full.batches][0]
    assert fake.queries[-1] == (
        "SELECT Id, Name, Industry, AnnualRevenue, NumberOfEmployees, IsDeleted, BillingCity, "
        "CreatedDate, SystemModstamp FROM Account"
    )
    assert table["Name"] == ["Alpha", "Beta", "Gamma"]  # two pages
    assert table["AnnualRevenue"][0] == Decimal("1500000")
    assert table["CreatedDate"][0] == datetime(2026, 1, 1, tzinfo=UTC)
    assert full.final_state() == {}

    # Merge: from the newest SystemModstamp seen, rounded down to the second, oldest first.
    state = {"kind": "datetime", "cursor": "2026-01-16T08:00:00+00:00"}
    merge = connector(fake).read(
        "Account",
        RefreshMode.MERGE,
        state,
        ReadOptions(cursor_field="SystemModstamp", primary_key=("Id",)),
    )
    changed = [b.to_pydict() for b in merge.batches][0]
    assert fake.queries[-1].endswith(
        "FROM Account WHERE SystemModstamp >= 2026-01-16T08:00:00Z ORDER BY SystemModstamp ASC"
    )
    assert changed["Name"] == ["Beta", "Gamma"]
    assert merge.final_state() == {"kind": "datetime", "cursor": "2026-01-17T09:30:00.250000+00:00"}
    with pytest.raises(ConnectorError, match="append refresh is not offered"):
        connector(fake).read("Account", RefreshMode.APPEND, None)


def test_salesforce_errors_are_explained() -> None:
    fake = FakeSalesforce()
    fake.token_error = {
        "error": "invalid_client",
        "error_description": "invalid client credentials",
    }
    result = connector(fake).test_connection()
    assert (
        not result.ok
        and "did not accept the sign-in" in result.message
        and "invalid client credentials" in result.message
    )
    fake = FakeSalesforce()
    fake.query_error = [
        {"errorCode": "INVALID_FIELD", "message": "No such column 'Nope' on entity 'Account'."}
    ]
    with pytest.raises(ConnectorError, match="No such column 'Nope'"):
        list(connector(fake).read("Account", RefreshMode.FULL, None).batches)
    wrong = connector(FakeSalesforce(), my_domain="https://login.salesforce.com").test_connection()
    assert not wrong.ok and "My Domain URL should look like" in wrong.message
    # The form asks for what each way of signing in needs.
    schema = SalesforceConnector.config_schema()
    errors = [
        e.message
        for e in Draft202012Validator(schema).iter_errors(
            {"my_domain": f"https://{HOST}", "auth": "password", "username": "u"}
        )
    ]
    assert any("'password' is a required property" in m for m in errors)
    assert any("'security_token' is a required property" in m for m in errors)
    assert not list(Draft202012Validator(schema).iter_errors(CLIENT))
    assert {n for n, p in schema["properties"].items() if p.get("x-secret")} == {
        "client_secret",
        "password",
        "security_token",
    }


def test_an_object_becomes_a_merged_dataset_through_the_app(
    client_for: ClientFor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSalesforce()
    monkeypatch.setattr(salesforce, "DEFAULT_SESSION", lambda: session_for(fake))
    client = client_for()
    connection = connect(client, "salesforce", CLIENT)
    assert ok(client.post(f"/api/connections/{connection}/test"))["ok"] is True
    dataset = make_dataset(
        client, connection, "Account", name="sf_accounts",
        refreshMode="merge", cursorField="SystemModstamp", primaryKey=["Id"],
    )  # fmt: skip
    run_jobs(client)
    first = ok(client.get(f"/api/datasets/{dataset['id']}/preview"))
    assert first["rowCount"] == 3
    # Beta is renamed and Delta is new; the next refresh asks only for what changed.
    fake.accounts[1] = account(2, "Beta renamed", "2026-01-18T00:00:00.000+0000")
    fake.accounts.append(account(4, "Delta", "2026-01-18T00:00:01.000+0000"))
    refresh(client, dataset["id"])
    assert "WHERE SystemModstamp >= 2026-01-17T09:30:00Z" in fake.queries[-1]
    rows = rows_of(client, dataset["id"])
    assert sorted(r["Name"] for r in rows) == ["Alpha", "Beta renamed", "Delta", "Gamma"]
    assert all(isinstance(r["CreatedDate"], str) for r in rows)
