"""Google Sheets connector: each tab of one spreadsheet is a stream.

Two ways to sign in, both read-only:

- `oauth` (the default): the person's own Google account, through their own
  OAuth client (the credentials.json of a Desktop or Web client from Google
  Cloud). The consent flow is run by `opsis.services.google_oauth`, which keeps
  the refresh token, encrypted, in the connection's secrets under `__oauth__`.
  Scopes: spreadsheets.readonly, and drive.metadata.readonly to list the
  spreadsheets the account can open and to show which account it is.
- `service_account`: a service account's JSON key, with the spreadsheet shared
  with the account's address as a Viewer.

The Sheets and Drive REST APIs are called directly; the token comes from
Google's auth library. If Google refuses a saved refresh token (invalid_grant),
`ReconnectNeeded` is raised so the app can ask the person to reconnect.

Values are read unformatted. A column's type comes from the cells: numbers stay
numbers (whole numbers as integers when every value is whole), true and false
become booleans, and cells formatted as dates or date-times become dates and
timestamps (wall-clock time in the spreadsheet's timezone). Anything mixed is
text. A sheet has no reliable change marker, so only full refresh is offered.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import quote

import httpx2
import pyarrow as pa

from opsis.connectors.base import (
    ColumnSpec,
    ColumnType,
    ConnectorError,
    ReadOptions,
    ReadResult,
    ReconnectNeeded,
    RefreshMode,
    StreamSpec,
    TestResult,
)
from opsis.connectors.tabular import rows_to_batch, unique_names

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
LIST_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
OAUTH_SCOPES = (SCOPE, LIST_SCOPE)
# Granted scopes that are enough to read spreadsheets, and to list them.
READ_SCOPES = frozenset(
    {
        SCOPE,
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive",
    }
)
LIST_SCOPES = frozenset(
    {
        LIST_SCOPE,
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive",
    }
)
SHEETS_BASE = "https://sheets.googleapis.com"
DRIVE_BASE = "https://www.googleapis.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
RECONNECT = (
    "Google no longer accepts this connection's sign-in (invalid_grant). Its access may "
    "have been removed, the account's password changed, or, for an OAuth app whose "
    "publishing status is Testing, its refresh token expired after 7 days. Reconnect the "
    "Google account."
)
# Rows read to see each column's cell formats, and rows fetched per request.
SAMPLE_ROWS = 200
PAGE_ROWS = 10_000
# Sheets counts dates in days from 30 December 1899.
SERIAL_EPOCH = datetime(1899, 12, 30)
ID_IN_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")

Token = Callable[[], str]
# Tests point these at a fake Google: the app builds connectors from config only.
DEFAULT_TRANSPORT: httpx2.BaseTransport | None = None
DEFAULT_TOKEN: Token | None = None


def spreadsheet_id(text: str) -> str | None:
    """The spreadsheet's ID, from its URL or as given."""
    text = text.strip()
    found = ID_IN_URL.search(text)
    if found:
        return found.group(1)
    return text if BARE_ID.match(text) else None


def client_config(text: Any) -> tuple[str, dict[str, Any]]:
    """The OAuth client in a credentials.json: ("installed" or "web", its settings)."""
    try:
        document = json.loads(str(text or ""))
    except ValueError:
        document = None
    if isinstance(document, dict) and document.get("type") == "service_account":
        raise ConnectorError(
            "This is a service account key, not an OAuth client. Choose service_account "
            "as the sign-in method, or download your OAuth client's JSON (credentials.json)."
        )
    if isinstance(document, dict) and "refresh_token" in document:
        raise ConnectorError(
            "This is a token.json, not the OAuth client. Upload credentials.json here; a "
            "token.json can be used after saving, under Connect Google account."
        )
    kind = next(
        (k for k in ("installed", "web") if isinstance(document, dict) and k in document), None
    )
    client = document.get(kind) if kind and isinstance(document, dict) else None
    if (
        not isinstance(client, dict)
        or not client.get("client_id")
        or not client.get("client_secret")
    ):
        raise ConnectorError(
            "This is not an OAuth client file. In Google Cloud, open APIs and services, "
            "Credentials, your OAuth 2.0 client ID (type Desktop app or Web application), "
            "and download its JSON."
        )
    assert kind is not None
    client = {
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": TOKEN_URI,
        **client,
    }
    return kind, client


def granted(scopes: Any) -> set[str]:
    """Scopes as a set, from a list or a space-separated string."""
    if isinstance(scopes, str):
        return set(scopes.split())
    return {str(s) for s in scopes or []}


def _a1(title: str) -> str:
    """A tab name as A1 notation needs it: quoted, with quotes doubled."""
    return "'" + title.replace("'", "''") + "'"


def _kind(cell: dict[str, Any]) -> str | None:
    """What one cell holds, judged by its value and its number format."""
    value = cell.get("effectiveValue") or {}
    if "boolValue" in value:
        return "bool"
    if "numberValue" in value:
        number_format = ((cell.get("effectiveFormat") or {}).get("numberFormat") or {}).get("type")
        if number_format == "DATE":
            return "date"
        if number_format == "DATE_TIME":
            return "datetime"
        if number_format == "TIME":
            return "time"
        return "integer" if float(value["numberValue"]).is_integer() else "number"
    if "stringValue" in value:
        return "text" if value["stringValue"].strip() else None
    return None  # empty, or an error such as #N/A


def _serial(value: Any) -> datetime:
    return SERIAL_EPOCH + timedelta(days=float(value))


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class GoogleSheetsConnector:
    id: ClassVar[str] = "gsheets"
    name: ClassVar[str] = "Google Sheets"
    version: ClassVar[str] = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["auth"],
            "properties": {
                "auth": {
                    "type": "string",
                    "title": "Sign in with",
                    "enum": ["oauth", "service_account"],
                    "x-enum-labels": {
                        "oauth": "My Google account (OAuth)",
                        "service_account": "A service account",
                    },
                    "default": "oauth",
                },
                "client_secrets": {
                    "type": "string",
                    "title": "OAuth client (credentials.json)",
                    "description": (
                        "The JSON file of your OAuth client ID from Google Cloud, of type "
                        "Desktop app (or Web application). After saving, choose Connect "
                        "Google account."
                    ),
                    "x-secret": True,
                    "x-file-text": ".json",
                    "x-show-when": {"auth": "oauth"},
                    "minLength": 2,
                },
                "service_account_key": {
                    "type": "string",
                    "title": "Service account key",
                    "description": (
                        "The JSON key file of a Google Cloud service account. Share the "
                        "spreadsheet with the account's email address as a Viewer."
                    ),
                    "x-secret": True,
                    "x-file-text": ".json",
                    "x-show-when": {"auth": "service_account"},
                    "minLength": 2,
                },
                "spreadsheet": {
                    "type": "string",
                    "title": "Spreadsheet",
                    "description": (
                        "Its URL, or its ID. Each tab becomes a table. With a connected "
                        "Google account you can also pick it from a list after saving."
                    ),
                    "minLength": 20,
                },
                "has_header": {
                    "type": "boolean",
                    "title": "First row is a header",
                    "default": True,
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"auth": {"const": "oauth"}}, "required": ["auth"]},
                    "then": {"required": ["client_secrets"]},
                },
                {
                    "if": {
                        "properties": {"auth": {"const": "service_account"}},
                        "required": ["auth"],
                    },
                    "then": {"required": ["service_account_key", "spreadsheet"]},
                },
            ],
        }

    def __init__(
        self,
        config: dict[str, Any],
        *,
        transport: httpx2.BaseTransport | None = None,
        token: Token | None = None,
    ) -> None:
        self.config = config
        self._id = spreadsheet_id(str(config.get("spreadsheet", "")))
        self._header = bool(config.get("has_header", True))
        # Connections from before the choice had a service account key and no method.
        self.auth = str(
            config.get("auth")
            or ("service_account" if config.get("service_account_key") else "oauth")
        )
        oauth = config.get("__oauth__")
        self._oauth: dict[str, Any] = oauth if isinstance(oauth, dict) else {}
        base = str(config.get("_google_api_base") or "").rstrip("/")
        self._sheets = f"{base or SHEETS_BASE}/v4/spreadsheets"
        self._drive = f"{base or DRIVE_BASE}/drive/v3"
        self._http = httpx2.Client(
            timeout=httpx2.Timeout(60.0, connect=10.0), transport=transport or DEFAULT_TRANSPORT
        )
        self._token_source = token or DEFAULT_TOKEN
        self._token: tuple[str, float] | None = None
        self._account = str(self._oauth.get("account") or "") if self.auth == "oauth" else ""

    # ---- signing in --------------------------------------------------------------------

    def _key(self) -> dict[str, Any]:
        try:
            key = json.loads(str(self.config.get("service_account_key", "")))
        except ValueError:
            key = None
        if (
            not isinstance(key, dict)
            or key.get("type") != "service_account"
            or not key.get("client_email")
            or not key.get("private_key")
        ):
            raise ConnectorError(
                "The service account key is not a JSON key file. In Google Cloud, open the "
                "service account, choose Keys, Add key, JSON, and use the whole downloaded file."
            )
        self._account = str(key["client_email"])
        return key

    def _bearer(self) -> str:
        if self._token is not None and self._token[1] > time.monotonic():
            return self._token[0]
        if self._token_source is not None:
            if self.auth == "service_account":
                self._key()
            token = self._token_source()
        elif self.auth == "oauth":
            token = self._oauth_token()
        else:
            token = self._google_token(self._key())
        self._token = (token, time.monotonic() + 30 * 60)
        return token

    def _oauth_token(self) -> str:
        """A fresh access token from the saved refresh token."""
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self._oauth.get("refresh_token"):
            raise ConnectorError(
                "This connection is not connected to a Google account yet. On the "
                "Connections page, choose Connect Google account."
            )
        _, client = client_config(self.config.get("client_secrets"))
        credentials = Credentials(  # type: ignore[no-untyped-call]
            None,
            refresh_token=str(self._oauth["refresh_token"]),
            token_uri=str(client["token_uri"]),
            client_id=str(client["client_id"]),
            client_secret=str(client["client_secret"]),
            scopes=sorted(granted(self._oauth.get("scopes"))) or None,
        )
        try:
            credentials.refresh(Request())  # type: ignore[no-untyped-call]
        except RefreshError as exc:
            text = str(exc)
            if "invalid_grant" in text:
                raise ReconnectNeeded(RECONNECT) from None
            if "invalid_client" in text or "unauthorized_client" in text:
                raise ConnectorError(
                    "Google did not accept the OAuth client in credentials.json: it may have "
                    "been deleted, or its secret reset. Upload its JSON again, then reconnect."
                ) from None
            raise ConnectorError(f"Google refused to renew the sign-in: {text[:200]}") from None
        except (TransportError, OSError) as exc:
            raise ConnectorError(f"Could not reach Google to sign in: {exc}") from None
        return str(credentials.token)

    def _google_token(self, key: dict[str, Any]) -> str:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        try:
            make = service_account.Credentials.from_service_account_info
            credentials = make(key, scopes=[SCOPE])  # type: ignore[no-untyped-call]
            credentials.refresh(Request())
        except (GoogleAuthError, ValueError) as exc:
            raise ConnectorError(
                f"Google did not accept the service account key ({type(exc).__name__}). Check "
                "that the key has not been deleted or disabled in Google Cloud."
            ) from None
        except OSError as exc:
            raise ConnectorError(f"Could not reach Google to sign in: {exc}") from None
        return str(credentials.token)

    # ---- the API -------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._id is None:
            raise ConnectorError(
                "Choose a spreadsheet first: paste its URL"
                + (", or pick it from your list" if self.auth == "oauth" else "")
                + "."
                if not str(self.config.get("spreadsheet") or "").strip()
                else "This is not a spreadsheet URL or ID. Copy the address of the "
                "spreadsheet from your browser."
            )
        return self._call(f"{self._sheets}/{self._id}{path}", params, "Google Sheets")

    def _call(self, url: str, params: dict[str, Any], service: str) -> dict[str, Any]:
        try:
            response = self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {self._bearer()}"}
            )
        except httpx2.HTTPError as exc:
            raise ConnectorError(f"Could not reach {service}: {type(exc).__name__}.") from None
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            return body
        raise ConnectorError(self._explain(response, service))

    # ---- the account and its spreadsheets (OAuth) ------------------------------------------

    def about(self) -> dict[str, str]:
        """The signed-in account: {"email", "name"} (needs a Drive scope)."""
        body = self._call(
            f"{self._drive}/about", {"fields": "user(emailAddress,displayName)"}, "Google Drive"
        )
        user = body.get("user") or {}
        return {
            "email": str(user.get("emailAddress") or ""),
            "name": str(user.get("displayName") or ""),
        }

    def spreadsheets(self, search: str | None = None) -> list[dict[str, Any]]:
        """Spreadsheets the account can open, newest first (needs a Drive scope)."""
        query = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        if search and search.strip():
            text = search.strip()[:100].replace("\\", "\\\\").replace("'", "\\'")
            query += f" and name contains '{text}'"
        body = self._call(
            f"{self._drive}/files",
            {
                "q": query,
                "fields": "files(id,name,modifiedTime,owners(displayName,emailAddress))",
                "orderBy": "modifiedTime desc",
                "pageSize": 100,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
            "Google Drive",
        )
        return [
            {
                "id": str(f.get("id")),
                "name": str(f.get("name") or ""),
                "modifiedTime": f.get("modifiedTime"),
                "owner": next(
                    (
                        str(o.get("emailAddress") or o.get("displayName"))
                        for o in f.get("owners") or []
                    ),
                    None,
                ),
            }
            for f in body.get("files") or []
        ]

    def _explain(self, response: httpx2.Response, service: str = "Google Sheets") -> str:
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        message = str(error.get("message") or response.text)[:300]
        status = response.status_code
        oauth = self.auth == "oauth"
        if status == 403 and ("has not been used" in message or "disabled" in message):
            api = "Google Drive API" if service == "Google Drive" else "Google Sheets API"
            whose = "your OAuth client's" if oauth else "the service account's"
            return (
                f"The {api} is not enabled in {whose} Google Cloud project. Enable it under "
                "APIs and services, Library, then try again."
            )
        if status == 403 and "insufficient" in message.lower():
            return (
                f"This sign-in may not use {service} for that: the scope was not granted. "
                "Reconnect the Google account and allow every permission asked for."
            )
        if status == 403:
            if oauth:
                who = self._account or "the connected Google account"
                return (
                    f"{who} cannot open this spreadsheet. Check that it is shared with that "
                    "account."
                )
            who = self._account or "the service account"
            return (
                f"The spreadsheet is not shared with {who}. Share it with that address as a Viewer."
            )
        if status == 404:
            return "There is no spreadsheet with this ID. Check the URL."
        if status == 401:
            return "Google did not accept the sign-in. Try again, or reconnect the Google account."
        if status == 429:
            return f"{service} is limiting requests right now. Try again in a minute."
        return f"{service} returned an error ({status}): {message}"

    def _tabs(self) -> tuple[str, list[dict[str, Any]]]:
        body = self._get(
            "",
            {
                "fields": "properties(title),sheets(properties(sheetId,title,sheetType,"
                "gridProperties(rowCount,columnCount)))"
            },
        )
        tabs = [
            s["properties"]
            for s in body.get("sheets") or []
            if (s.get("properties") or {}).get("sheetType", "GRID") == "GRID"
        ]
        return str((body.get("properties") or {}).get("title") or ""), tabs

    def _tab(self, title: str) -> dict[str, Any]:
        _, tabs = self._tabs()
        for tab in tabs:
            if tab.get("title") == title:
                return tab
        raise ConnectorError(f"There is no tab called {title!r} in the spreadsheet any more.")

    def _sample(self, title: str) -> tuple[list[Any], list[set[str]]]:
        """The header row and, per column, the kinds of value seen in the first rows."""
        body = self._get(
            "",
            {
                "ranges": f"{_a1(title)}!1:{SAMPLE_ROWS + 1}",
                "includeGridData": "true",
                "fields": "sheets(data(rowData(values(effectiveValue,"
                "effectiveFormat(numberFormat(type))))))",
            },
        )
        data = ((body.get("sheets") or [{}])[0].get("data") or [{}])[0]
        rows = [r.get("values") or [] for r in data.get("rowData") or []]
        header: list[Any] = []
        if self._header and rows:
            header = [next(iter((c.get("effectiveValue") or {}).values()), None) for c in rows[0]]
            rows = rows[1:]
        width = max([len(header), *(len(r) for r in rows)], default=0)
        kinds: list[set[str]] = [set() for _ in range(width)]
        for row in rows:
            for index, cell in enumerate(row):
                kind = _kind(cell)
                if kind:
                    kinds[index].add(kind)
        return header, kinds

    def _values(self, title: str, row_count: int) -> list[list[Any]]:
        """Every row of a tab, unformatted, a page at a time."""
        rows: list[list[Any]] = []
        for start in range(1, max(row_count, 1) + 1, PAGE_ROWS):
            end = start + PAGE_ROWS - 1
            body = self._get(
                f"/values/{quote(f'{_a1(title)}!{start}:{end}', safe='')}",
                {
                    "valueRenderOption": "UNFORMATTED_VALUE",
                    "dateTimeRenderOption": "SERIAL_NUMBER",
                    "majorDimension": "ROWS",
                },
            )
            page = body.get("values") or []
            # A page ends at its last non-empty row: keep row positions for the next one.
            rows += page + [[] for _ in range(min(end, row_count) - start + 1 - len(page))]
        return rows

    # ---- types -------------------------------------------------------------------------

    @staticmethod
    def _type(kinds: set[str], values: list[Any] | None) -> ColumnType:
        """A column's type from the kinds seen in the sample and, when reading, all
        its values (a later row can hold text, or a fraction, in a number column)."""
        if not kinds:
            return ColumnType.STRING
        if kinds == {"bool"}:
            return ColumnType.BOOLEAN
        if kinds == {"date"}:
            return ColumnType.DATE
        if kinds <= {"date", "datetime"}:
            return ColumnType.TIMESTAMP
        if kinds <= {"integer", "number"}:
            if values is None:
                return ColumnType.INTEGER if kinds == {"integer"} else ColumnType.FLOAT
            numbers = [v for v in values if v is not None and v != ""]
            if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in numbers):
                return ColumnType.STRING  # text further down than the sample
            whole = all(float(v).is_integer() for v in numbers)
            return ColumnType.INTEGER if whole else ColumnType.FLOAT
        return ColumnType.STRING

    @staticmethod
    def _convert(value: Any, column: ColumnSpec) -> Any:
        if value is None or value == "":
            return None
        match column.type:
            case ColumnType.DATE:
                return _serial(value).date() if isinstance(value, int | float) else None
            case ColumnType.TIMESTAMP:
                return _serial(value) if isinstance(value, int | float) else None
            case ColumnType.INTEGER:
                return int(value) if isinstance(value, int | float) else None
            case ColumnType.FLOAT:
                return float(value) if isinstance(value, int | float) else None
            case ColumnType.BOOLEAN:
                return value if isinstance(value, bool) else None
        if column.source_type == "time" and isinstance(value, int | float):
            return _serial(value).strftime("%H:%M:%S")  # a time of day: a fraction of a day
        return _text(value)

    def _columns(self, title: str, values: list[list[Any]] | None) -> list[ColumnSpec]:
        header, kinds = self._sample(title)
        width = max([len(kinds), *(len(r) for r in values or [])], default=0)
        names = (
            unique_names(header + [None] * (width - len(header)))
            if self._header
            else [f"column_{i + 1}" for i in range(width)]
        )
        columns = []
        for index, name in enumerate(names):
            column_kinds = kinds[index] if index < len(kinds) else set()
            column_values = (
                None  # discovery: the sample decides
                if values is None
                else [row[index] if index < len(row) else None for row in values]
            )
            columns.append(
                ColumnSpec(
                    name=name,
                    type=self._type(column_kinds, column_values),
                    source_type="/".join(sorted(column_kinds)) or "empty",
                )
            )
        return columns

    # ---- Connector protocol ----------------------------------------------------------

    def test_connection(self) -> TestResult:
        try:
            title, tabs = self._tabs()
        except ReconnectNeeded:
            raise  # the app marks the connection; the message says what to do
        except ConnectorError as exc:
            return TestResult(ok=False, message=str(exc))
        who = self._account or ("the connected Google account" if self.auth == "oauth" else "?")
        return TestResult(
            ok=True,
            message=f"Found {title!r} with {len(tabs)} tab(s), reading as {who}.",
        )

    def discover(self) -> list[StreamSpec]:
        _, tabs = self._tabs()
        streams = []
        for tab in tabs:
            title = str(tab["title"])
            rows = int((tab.get("gridProperties") or {}).get("rowCount") or 0)
            streams.append(
                StreamSpec(
                    name=title,
                    label=title,
                    columns=tuple(self._columns(title, None)),
                    row_count_estimate=max(rows - (1 if self._header else 0), 0),
                )
            )
        return streams

    def read(
        self,
        stream: str,
        mode: RefreshMode,
        state: dict[str, Any] | None,
        options: ReadOptions | None = None,
    ) -> ReadResult:
        options = options or ReadOptions()
        tab = self._tab(stream)
        row_count = int((tab.get("gridProperties") or {}).get("rowCount") or 0)
        rows = self._values(stream, row_count)
        if self._header:
            rows = rows[1:]
        rows = [r for r in rows if any(v is not None and v != "" for v in r)]
        columns = self._columns(stream, rows)
        keep = [
            i for i, c in enumerate(columns) if not options.columns or c.name in options.columns
        ]
        columns = [columns[i] for i in keep]
        converted = [
            tuple(
                self._convert(row[i] if i < len(row) else None, columns[n])
                for n, i in enumerate(keep)
            )
            for row in rows
        ]

        def batches() -> Iterator[pa.RecordBatch]:
            if not converted:
                yield rows_to_batch(columns, [])
            for start in range(0, len(converted), options.batch_size):
                yield rows_to_batch(columns, converted[start : start + options.batch_size])

        return ReadResult(batches())

    def close(self) -> None:
        self._http.close()


CONNECTOR = GoogleSheetsConnector
