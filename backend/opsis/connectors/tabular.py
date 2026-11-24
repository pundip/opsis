"""Helpers shared by connectors: logical types to Arrow, and rows to record batches."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa

from opsis.connectors.base import ColumnSpec, ColumnType
from opsis.connectors.convert import Conversion, arrow_type

__all__ = ["arrow_schema", "arrow_type", "column_type_of", "rows_to_batch", "unique_names"]


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


def rows_to_batch(
    columns: Sequence[ColumnSpec],
    rows: Sequence[Sequence[Any]],
    conversion: Conversion | None = None,
) -> pa.RecordBatch:
    """Build a typed record batch from row tuples. An empty `rows` gives an empty batch.

    A value that cannot become its column's type is stored as NULL and counted in
    `conversion`: pass the read's ReadOptions.conversion, so the refresh reports it.
    """
    return (conversion or Conversion()).batch(columns, rows)


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
