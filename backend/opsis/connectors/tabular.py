"""Helpers shared by connectors: logical types to Arrow, and rows to record batches."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa

from opsis.connectors.base import ColumnSpec, ColumnType


def arrow_type(column: ColumnSpec) -> pa.DataType:
    match column.type:
        case ColumnType.INTEGER:
            return pa.int64()
        case ColumnType.FLOAT:
            return pa.float64()
        case ColumnType.DECIMAL:
            # Without a known scale a decimal cannot be stored exactly.
            return pa.float64() if column.scale is None else pa.decimal128(38, column.scale)
        case ColumnType.BOOLEAN:
            return pa.bool_()
        case ColumnType.DATE:
            return pa.date32()
        case ColumnType.TIMESTAMP:
            return pa.timestamp("us", tz="UTC") if column.tz_aware else pa.timestamp("us")
        case _:
            return pa.string()


def arrow_schema(columns: Sequence[ColumnSpec]) -> pa.Schema:
    return pa.schema([pa.field(c.name, arrow_type(c)) for c in columns])


def column_type_of(dtype: pa.DataType) -> ColumnType:
    """The logical type for an Arrow type (used when describing snapshots)."""
    if pa.types.is_boolean(dtype):
        return ColumnType.BOOLEAN
    if pa.types.is_integer(dtype):
        return ColumnType.INTEGER
    if pa.types.is_floating(dtype):
        return ColumnType.FLOAT
    if pa.types.is_decimal(dtype):
        return ColumnType.DECIMAL
    if pa.types.is_date(dtype):
        return ColumnType.DATE
    if pa.types.is_timestamp(dtype):
        return ColumnType.TIMESTAMP
    return ColumnType.STRING


def _coerce(value: Any, column: ColumnSpec) -> Any:
    if value is None:
        return None
    match column.type:
        case ColumnType.STRING:
            if isinstance(value, str):
                return value
            if isinstance(value, bytes | bytearray | memoryview):
                return bytes(value).decode("utf-8", errors="replace")
            return str(value)
        case ColumnType.JSON:
            return value if isinstance(value, str) else json.dumps(value, default=str)
        case ColumnType.INTEGER:
            return int(value)
        case ColumnType.FLOAT:
            return float(value)
        case ColumnType.DECIMAL:
            if column.scale is None:
                return float(value)
            return value if isinstance(value, Decimal) else Decimal(str(value))
        case ColumnType.BOOLEAN:
            if isinstance(value, bytes):  # MySQL BIT(1)
                return value not in (b"\x00", b"")
            return bool(value)
        case ColumnType.TIMESTAMP:
            if isinstance(value, date) and not isinstance(value, datetime):
                value = datetime(value.year, value.month, value.day)
            if not isinstance(value, datetime):
                return value
            if column.tz_aware:  # an instant: keep it, in UTC
                return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            # Wall-clock time: drop any offset rather than shifting the clock.
            return value.replace(tzinfo=None)
        case ColumnType.DATE:
            return value.date() if isinstance(value, datetime) else value
    return value


def rows_to_batch(columns: Sequence[ColumnSpec], rows: Sequence[Sequence[Any]]) -> pa.RecordBatch:
    """Build a typed record batch from row tuples. An empty `rows` gives an empty batch."""
    arrays = [
        pa.array([_coerce(row[i], column) for row in rows], type=arrow_type(column))
        for i, column in enumerate(columns)
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=arrow_schema(columns))


def unique_names(raw: list[Any]) -> list[str]:
    """Column names from a header row: blanks named column_N, repeats numbered."""
    names: list[str] = []
    for index, value in enumerate(raw):
        name = str(value).strip() if value is not None else ""
        name = name or f"column_{index + 1}"
        candidate, n = name, 2
        while candidate in names:
            candidate, n = f"{name}_{n}", n + 1
        names.append(candidate)
    return names
