"""Salesforce connector: each object (Account, Opportunity, a custom object) is a stream.

SOQL through the REST API, with simple-salesforce. Two ways to sign in, both to
your My Domain: an External Client App (or Connected App) using the OAuth client
credentials flow, which is what Salesforce recommends for an integration, or a
username, password and security token.

Incremental refresh merges by Id, following SystemModstamp (the brief's choice:
it changes whenever a record or the system touches it). SOQL date-time literals
have no fraction of a second, so each refresh reads the last second again and
the merge removes the repeats. Records deleted in Salesforce stay in the
snapshot until a full refresh, as with the SQL connectors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pyarrow as pa
import requests

from opsis.connectors.base import (
    ColumnSpec,
    ColumnType,
    ConnectorError,
    ReadOptions,
    ReadResult,
    RefreshMode,
    StreamSpec,
    TestResult,
)
from opsis.connectors.sql_base import decode_cursor, encode_cursor
from opsis.connectors.tabular import rows_to_batch

# Offered as tables when the connection does not name its own list (plus custom objects).
STANDARD_OBJECTS = (
    "Account",
    "Contact",
    "Lead",
    "Opportunity",
    "OpportunityLineItem",
    "Campaign",
    "CampaignMember",
    "Case",
    "Task",
    "Event",
    "User",
    "Product2",
)
MAX_CUSTOM_OBJECTS = 50
CURSORS = ("SystemModstamp", "LastModifiedDate", "CreatedDate")
# Compound fields repeat their parts (BillingCity and so on); files are not data.
SKIPPED_TYPES = {"address", "location", "base64", "complexvalue"}
TEXT_TYPES = {
    "id",
    "reference",
    "string",
    "picklist",
    "multipicklist",
    "textarea",
    "phone",
    "url",
    "email",
    "combobox",
    "encryptedstring",
    "anytype",
    "time",
}
# REST queries are GET requests: Salesforce refuses very long URLs.
MAX_QUERY_LENGTH = 15_000
REQUEST_TIMEOUT = 120.0
# Tests point these at a fake Salesforce: the app builds connectors from config only.
DEFAULT_SESSION: Callable[[], requests.Session] | None = None
DEFAULT_LOGIN: Callable[[dict[str, Any], requests.Session], Any] | None = None


def _column(field: dict[str, Any]) -> ColumnSpec | None:
    kind = str(field.get("type") or "").lower()
    if kind in SKIPPED_TYPES:
        return None
    name = str(field["name"])
    if kind == "boolean":
        ctype, scale = ColumnType.BOOLEAN, None
    elif kind in ("int", "long"):
        ctype, scale = ColumnType.INTEGER, None
    elif kind == "currency":
        ctype, scale = ColumnType.DECIMAL, int(field.get("scale") or 2)
    elif kind in ("double", "percent"):
        ctype, scale = ColumnType.FLOAT, None
    elif kind == "date":
        ctype, scale = ColumnType.DATE, None
    elif kind == "datetime":
        ctype, scale = ColumnType.TIMESTAMP, None
    else:
        ctype, scale = ColumnType.STRING, None
    return ColumnSpec(
        name=name,
        type=ctype,
        nullable=bool(field.get("nillable", True)),
        source_type=kind,
        scale=scale,
        # Salesforce date-times are instants, sent in UTC.
        tz_aware=ctype == ColumnType.TIMESTAMP,
    )


def _value(raw: Any, column: ColumnSpec) -> Any:
    if raw is None:
        return None
    match column.type:
        case ColumnType.DATE:
            return date.fromisoformat(str(raw)[:10])
        case ColumnType.TIMESTAMP:
            text = str(raw).replace("Z", "+00:00")
            if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":  # +0000 to +00:00
                text = f"{text[:-2]}:{text[-2:]}"
            return datetime.fromisoformat(text).astimezone(UTC)
        case ColumnType.DECIMAL:
            return Decimal(str(raw))
    return raw


def _soql_datetime(value: datetime) -> str:
    """A SOQL date-time literal, rounded down to the second (SOQL has no fractions)."""
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Session(requests.Session):
    """simple-salesforce sets no timeout: every request here has one."""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        return super().request(*args, **kwargs)


class SalesforceConnector:
    id: ClassVar[str] = "salesforce"
    name: ClassVar[str] = "Salesforce"
    version: ClassVar[str] = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["my_domain", "auth"],
            "properties": {
                "my_domain": {
                    "type": "string",
                    "title": "My Domain URL",
                    "description": (
                        "From Setup, My Domain, for example "
                        "https://acme.my.salesforce.com. A sandbox's ends in "
                        ".sandbox.my.salesforce.com."
                    ),
                    "minLength": 8,
                },
                "auth": {
                    "type": "string",
                    "title": "Sign in with",
                    "enum": ["client_credentials", "password"],
                    "default": "client_credentials",
                    "description": (
                        "client_credentials: an External Client App with the client "
                        "credentials flow (recommended). password: a username, password and "
                        "security token."
                    ),
                },
                "client_id": {
                    "type": "string",
                    "title": "Consumer key",
                    "description": "For client_credentials: the app's consumer key.",
                },
                "client_secret": {
                    "type": "string",
                    "title": "Consumer secret",
                    "description": "For client_credentials: the app's consumer secret.",
                    "x-secret": True,
                },
                "username": {
                    "type": "string",
                    "title": "Username",
                    "description": "For password: the integration user's username.",
                },
                "password": {
                    "type": "string",
                    "title": "Password",
                    "description": "For password: that user's password.",
                    "x-secret": True,
                },
                "security_token": {
                    "type": "string",
                    "title": "Security token",
                    "description": (
                        "For password: from the user's settings, Reset My Security Token."
                    ),
                    "x-secret": True,
                },
                "objects": {
                    "type": "string",
                    "title": "Objects",
                    "description": (
                        "Optional. Object API names to offer as tables, separated by commas, "
                        "for example Account, Opportunity, Donation__c. Empty: the common "
                        "standard objects and your custom objects."
                    ),
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"auth": {"const": "password"}}},
                    "then": {"required": ["username", "password", "security_token"]},
                    "else": {"required": ["client_id", "client_secret"]},
                }
            ],
        }

    def __init__(
        self,
        config: dict[str, Any],
        *,
        session: requests.Session | None = None,
        login: Callable[[dict[str, Any], requests.Session], Any] | None = None,
    ) -> None:
        self.config = config
        self._session = session or (DEFAULT_SESSION() if DEFAULT_SESSION else _Session())
        self._login = login or DEFAULT_LOGIN
        self._client: Any = None
        self._described: dict[str, dict[str, Any]] = {}

    # ---- signing in ------------------------------------------------------------------

    def _domain(self) -> str:
        """The My Domain host without ".salesforce.com", as simple-salesforce wants it."""
        raw = str(self.config.get("my_domain", "")).strip()
        host = (urlsplit(raw).hostname or "") if "//" in raw else raw.split("/")[0]
        host = host.lower().rstrip(".")
        if not host.endswith(".salesforce.com") or host in ("login.salesforce.com",):
            raise ConnectorError(
                "My Domain URL should look like https://acme.my.salesforce.com (Setup, My Domain)."
            )
        return host.removesuffix(".salesforce.com")

    def _sf(self) -> Any:
        if self._client is not None:
            return self._client
        from simple_salesforce.api import Salesforce
        from simple_salesforce.exceptions import SalesforceAuthenticationFailed, SalesforceError

        try:
            if self._login is not None:
                self._client = self._login(self.config, self._session)
            elif self.config.get("auth") == "password":
                self._client = Salesforce(
                    username=str(self.config.get("username", "")),
                    password=str(self.config.get("password", "")),
                    security_token=str(self.config.get("security_token", "")),
                    domain=self._domain(),
                    session=self._session,
                    parse_float=Decimal,
                )
            else:
                self._client = Salesforce(
                    consumer_key=str(self.config.get("client_id", "")),
                    consumer_secret=str(self.config.get("client_secret", "")),
                    domain=self._domain(),
                    session=self._session,
                    parse_float=Decimal,
                )
        except SalesforceAuthenticationFailed as exc:
            raise ConnectorError(f"Salesforce did not accept the sign-in: {exc}") from None
        except SalesforceError as exc:
            raise ConnectorError(f"Salesforce refused the sign-in: {exc}") from None
        except requests.RequestException as exc:
            raise ConnectorError(
                f"Could not reach Salesforce at {self.config.get('my_domain')}: "
                f"{type(exc).__name__}."
            ) from None
        return self._client

    def _call(self, work: Callable[[Any], Any]) -> Any:
        from simple_salesforce.exceptions import SalesforceError

        sf = self._sf()
        try:
            return work(sf)
        except SalesforceError as exc:
            content: list[Any] = exc.content if isinstance(exc.content, list) else []
            detail = "; ".join(str(e.get("message", e)) for e in content if isinstance(e, dict))
            raise ConnectorError(f"Salesforce returned an error: {detail or exc}") from None
        except requests.RequestException as exc:
            raise ConnectorError(f"Could not reach Salesforce: {type(exc).__name__}.") from None

    # ---- objects -----------------------------------------------------------------------

    def _object_names(self) -> list[str]:
        listed = [n.strip() for n in str(self.config.get("objects") or "").split(",") if n.strip()]
        described = self._call(lambda sf: sf.describe())
        queryable = {
            o["name"]: o
            for o in described.get("sobjects") or []
            if o.get("queryable") and o.get("retrieveable", True)
        }
        if listed:
            missing = [n for n in listed if n not in queryable]
            if missing:
                raise ConnectorError(
                    f"Salesforce has no object it lets this user read called {', '.join(missing)}."
                )
            return listed
        custom = sorted(n for n in queryable if n.endswith("__c"))[:MAX_CUSTOM_OBJECTS]
        return [n for n in STANDARD_OBJECTS if n in queryable] + custom

    def _describe(self, name: str) -> dict[str, Any]:
        if name not in self._described:
            self._described[name] = self._call(lambda sf: getattr(sf, name).describe())
        return self._described[name]

    def _columns(self, name: str) -> list[ColumnSpec]:
        fields = self._describe(name).get("fields") or []
        return [c for c in (_column(f) for f in fields) if c is not None]

    def _spec(self, name: str) -> StreamSpec:
        described = self._describe(name)
        columns = self._columns(name)
        names = {c.name for c in columns}
        cursors = tuple(c for c in CURSORS if c in names)
        return StreamSpec(
            name=name,
            label=str(described.get("labelPlural") or described.get("label") or name),
            columns=tuple(columns),
            primary_key=("Id",) if "Id" in names else (),
            cursor_candidates=cursors,
            supported_modes=(RefreshMode.FULL, RefreshMode.MERGE)
            if cursors and "Id" in names
            else (RefreshMode.FULL,),
        )

    # ---- Connector protocol ----------------------------------------------------------

    def test_connection(self) -> TestResult:
        try:
            org = self._call(
                lambda sf: sf.query(
                    "SELECT Name, OrganizationType, IsSandbox FROM Organization LIMIT 1"
                )
            )
        except ConnectorError as exc:
            return TestResult(ok=False, message=str(exc))
        record = (org.get("records") or [{}])[0]
        kind = str(record.get("OrganizationType") or "unknown edition")
        sandbox = " (a sandbox)" if record.get("IsSandbox") else ""
        return TestResult(
            ok=True,
            message=f"Connected to {record.get('Name') or 'the org'}, {kind}{sandbox}.",
            details={"organizationType": kind, "isSandbox": bool(record.get("IsSandbox"))},
        )

    def discover(self) -> list[StreamSpec]:
        return [self._spec(name) for name in self._object_names()]

    def read(
        self,
        stream: str,
        mode: RefreshMode,
        state: dict[str, Any] | None,
        options: ReadOptions | None = None,
    ) -> ReadResult:
        options = options or ReadOptions()
        spec = self._spec(stream)
        by_name = {c.name: c for c in spec.columns}
        if mode == RefreshMode.APPEND:
            raise ConnectorError(
                "Salesforce records change in place, so append refresh is not offered. Use "
                "merge (by Id), or full."
            )
        cursor = (options.cursor_field or "SystemModstamp") if mode == RefreshMode.MERGE else None
        if cursor is not None and (
            by_name.get(cursor) is None or by_name[cursor].type != ColumnType.TIMESTAMP
        ):
            raise ConnectorError(f"{cursor} is not a date-time field of {stream}.")
        wanted = [c for c in spec.columns if not options.columns or c.name in options.columns]
        for extra in ("Id", *([cursor] if cursor else [])):
            if extra in by_name and all(c.name != extra for c in wanted):
                wanted.append(by_name[extra])
        soql = f"SELECT {', '.join(c.name for c in wanted)} FROM {stream}"
        last = decode_cursor(state) if cursor else None
        if cursor and isinstance(last, datetime):
            soql += f" WHERE {cursor} >= {_soql_datetime(last)}"
        if cursor:
            soql += f" ORDER BY {cursor} ASC"
        if len(soql) > MAX_QUERY_LENGTH:
            raise ConnectorError(
                f"{stream} has too many fields to read in one query. Choose fewer columns for "
                "the dataset."
            )
        newest: list[datetime] = [last] if isinstance(last, datetime) else []

        def batches() -> Iterator[pa.RecordBatch]:
            rows: list[tuple[Any, ...]] = []
            produced = False
            records = self._call(lambda sf: sf.query_all_iter(soql))
            try:
                for record in records:
                    row = tuple(_value(record.get(c.name), c) for c in wanted)
                    rows.append(row)
                    if cursor is not None:
                        seen = row[wanted.index(by_name[cursor])]
                        if isinstance(seen, datetime) and (not newest or seen > newest[0]):
                            newest[:] = [seen]
                    if len(rows) >= options.batch_size:
                        produced = True
                        yield rows_to_batch(wanted, rows)
                        rows = []
            except ConnectorError:
                raise
            except Exception as exc:
                from simple_salesforce.exceptions import SalesforceError

                if isinstance(exc, SalesforceError | requests.RequestException):
                    raise ConnectorError(f"Salesforce stopped the read: {exc}") from None
                raise
            if rows or not produced:
                yield rows_to_batch(wanted, rows)

        def final_state() -> dict[str, Any]:
            if cursor is None or not newest:
                return {}
            return encode_cursor(newest[0]) or {}

        return ReadResult(batches(), final_state)

    def close(self) -> None:
        self._session.close()


CONNECTOR = SalesforceConnector
