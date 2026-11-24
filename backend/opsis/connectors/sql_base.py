"""Shared logic for SQL database connectors (PostgreSQL, MySQL).

Subclasses provide the connection, identifier quoting and discovery. This base
builds the SELECT safely: stream and column names are only ever taken from the
connector's own discovery result, never from user text, and cursor values are
bound as parameters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pyarrow as pa

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
from opsis.connectors.tabular import rows_to_batch

CURSOR_TYPES = {ColumnType.INTEGER, ColumnType.TIMESTAMP, ColumnType.DATE, ColumnType.DECIMAL}
CURSOR_VALUES = (datetime, date, int, float, Decimal)


def encode_cursor(value: Any) -> dict[str, Any] | None:
    """Turn the largest cursor value seen into JSON-safe state."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return {"kind": "datetime", "cursor": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "cursor": value.isoformat()}
    if isinstance(value, bool):
        raise ConnectorError("A boolean column cannot be used as a cursor.")
    if isinstance(value, int):
        return {"kind": "int", "cursor": str(value)}
    if isinstance(value, Decimal):
        return {"kind": "decimal", "cursor": str(value)}
    if isinstance(value, float):
        return {"kind": "float", "cursor": repr(value)}
    return {"kind": "str", "cursor": str(value)}


def decode_cursor(state: dict[str, Any] | None) -> Any:
    if not state or state.get("cursor") is None:
        return None
    raw, kind = str(state["cursor"]), state.get("kind")
    match kind:
        case "datetime":
            return datetime.fromisoformat(raw)
        case "date":
            return date.fromisoformat(raw)
        case "int":
            return int(raw)
        case "decimal":
            return Decimal(raw)
        case "float":
            return float(raw)
    return raw


def modes_for(
    columns: tuple[ColumnSpec, ...], primary_key: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[RefreshMode, ...]]:
    candidates = tuple(c.name for c in columns if c.type in CURSOR_TYPES)
    modes = [RefreshMode.FULL]
    if candidates:
        modes.append(RefreshMode.APPEND)
        if primary_key:
            modes.append(RefreshMode.MERGE)
    return candidates, tuple(modes)


class SqlConnector(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    version: ClassVar[str]

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._conn: Any = None
        self._streams: list[StreamSpec] | None = None

    # ---- provided by subclasses ------------------------------------------------

    @abstractmethod
    def _connect(self) -> Any:
        """Open a read-only connection."""

    @abstractmethod
    def _quote(self, identifier: str) -> str:
        """Quote one identifier for this database."""

    @abstractmethod
    def _table_sql(self, stream: str) -> str:
        """The quoted table reference for a discovered stream name."""

    @abstractmethod
    def _discover(self, conn: Any) -> list[StreamSpec]: ...

    @abstractmethod
    def _stream_cursor(self, conn: Any) -> Any:
        """A cursor that streams rows from the server instead of buffering them."""

    def _friendly(self, exc: Exception) -> str:
        return str(exc).strip() or type(exc).__name__

    # ---- Connector protocol ----------------------------------------------------

    def _connection(self) -> Any:
        if self._conn is None:
            try:
                self._conn = self._connect()
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError(f"Could not connect: {self._friendly(exc)}") from exc
        return self._conn

    def test_connection(self) -> TestResult:
        try:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchall()
        except ConnectorError as exc:
            return TestResult(ok=False, message=str(exc))
        except Exception as exc:
            return TestResult(ok=False, message=self._friendly(exc))
        return TestResult(ok=True, message="Connected.")

    def discover(self) -> list[StreamSpec]:
        if self._streams is None:
            try:
                self._streams = self._discover(self._connection())
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError(f"Could not list tables: {self._friendly(exc)}") from exc
        return self._streams

    def _spec(self, stream: str) -> StreamSpec:
        for spec in self.discover():
            if spec.name == stream:
                return spec
        raise ConnectorError(f"Table {stream!r} was not found in the source.")

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

        wanted = list(options.columns) if options.columns else [c.name for c in spec.columns]
        cursor_field = options.cursor_field if mode != RefreshMode.FULL else None
        if mode != RefreshMode.FULL and not cursor_field:
            raise ConnectorError(f"{mode.value} refresh needs a cursor column.")
        for name in (*wanted, *options.primary_key, *([cursor_field] if cursor_field else [])):
            if name not in by_name:
                raise ConnectorError(f"Column {name!r} was not found in {stream!r}.")
        for extra in (*options.primary_key, *([cursor_field] if cursor_field else [])):
            if extra not in wanted:
                wanted.append(extra)
        columns = [by_name[name] for name in wanted]

        select_list = ", ".join(self._quote(c.name) for c in columns)
        sql = f"SELECT {select_list} FROM {self._table_sql(stream)}"
        params: list[Any] = []
        last = decode_cursor(state) if cursor_field else None
        if cursor_field:
            if last is not None:
                sql += f" WHERE {self._quote(cursor_field)} > %s"
                params.append(last)
            sql += f" ORDER BY {self._quote(cursor_field)}"
        cursor_index = wanted.index(cursor_field) if cursor_field else -1
        seen: dict[str, Any] = {"max": last}
        conversion = options.conversion

        def track(rows: list[Any]) -> None:
            # Invalid values (a zero date comes back as text) cannot be a cursor.
            raw = [r[cursor_index] for r in rows]
            values = [v for v in raw if isinstance(v, CURSOR_VALUES) and not isinstance(v, bool)]
            if not values:
                return
            try:
                top = max(values)
                seen["max"] = top if seen["max"] is None else max(seen["max"], top)
            except TypeError as exc:
                raise ConnectorError(
                    f"The cursor column {cursor_field!r} of {stream!r} holds values that cannot "
                    f"be compared, such as {values[0]!r}. Choose another cursor column."
                ) from exc

        def batches() -> Iterator[pa.RecordBatch]:
            produced = False
            try:
                with self._stream_cursor(self._connection()) as cur:
                    cur.execute(sql, params)
                    while True:
                        rows = cur.fetchmany(options.batch_size)
                        if not rows:
                            break
                        if cursor_index >= 0:
                            track(rows)
                        produced = True
                        yield rows_to_batch(columns, rows, conversion)
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError(f"Reading {stream!r} failed: {self._friendly(exc)}") from exc
            if not produced:
                yield rows_to_batch(columns, [], conversion)

        def final_state() -> dict[str, Any]:
            return encode_cursor(seen["max"]) or {} if cursor_field else {}

        return ReadResult(batches(), final_state)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
