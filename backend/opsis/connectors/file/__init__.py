"""CSV and Excel upload connector.

The file is uploaded through the API first; this connector reads it from the
uploads directory. A CSV file is one stream. An Excel workbook has one stream
per sheet. Files are static, so only full refresh is supported.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, ClassVar

import duckdb
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
from opsis.connectors.tabular import arrow_schema, rows_to_batch

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
ALLOWED_SUFFIXES = CSV_SUFFIXES | EXCEL_SUFFIXES
DELIMITERS = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}


def _unique_names(raw: list[Any]) -> list[str]:
    names: list[str] = []
    for index, value in enumerate(raw):
        name = str(value).strip() if value is not None else ""
        name = name or f"column_{index + 1}"
        candidate, n = name, 2
        while candidate in names:
            candidate, n = f"{name}_{n}", n + 1
        names.append(candidate)
    return names


def _infer(values: list[Any]) -> ColumnType:
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return ColumnType.STRING
    if all(isinstance(v, bool) for v in present):
        return ColumnType.BOOLEAN
    if all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return ColumnType.INTEGER
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in present):
        return ColumnType.FLOAT
    if all(isinstance(v, datetime) for v in present):
        midnight = all(v.time() == time(0) for v in present)
        return ColumnType.DATE if midnight else ColumnType.TIMESTAMP
    if all(isinstance(v, date) for v in present):
        return ColumnType.DATE
    return ColumnType.STRING


class FileConnector:
    id: ClassVar[str] = "file"
    name: ClassVar[str] = "CSV or Excel file"
    version: ClassVar[str] = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["file"],
            "properties": {
                "file": {
                    "type": "string",
                    "title": "File",
                    "description": "A .csv, .tsv, .txt or .xlsx file.",
                    "x-upload": True,
                    "minLength": 1,
                },
                "has_header": {
                    "type": "boolean",
                    "title": "First row is a header",
                    "default": True,
                },
                "delimiter": {
                    "type": "string",
                    "title": "CSV delimiter",
                    "enum": ["auto", *DELIMITERS],
                    "default": "auto",
                },
            },
        }

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        uploads = Path(str(config.get("_uploads_dir", ""))).resolve()
        path = (uploads / str(config.get("file", ""))).resolve()
        # The stored value is "<upload id>/<file name>". Never read outside the uploads dir.
        self._path = path if path.is_relative_to(uploads) and path != uploads else None
        self._header = bool(config.get("has_header", True))

    def _file(self) -> Path:
        if self._path is None or not self._path.is_file():
            raise ConnectorError("The uploaded file was not found. Upload it again.")
        if self._path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ConnectorError(f"Files of type {self._path.suffix} are not supported.")
        return self._path

    @property
    def _is_excel(self) -> bool:
        return self._file().suffix.lower() in EXCEL_SUFFIXES

    # ---- CSV: DuckDB sniffs the dialect and types over the whole file ----------

    def _csv_relation(self, conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyRelation:
        options: dict[str, Any] = {"header": self._header, "sample_size": -1}
        delimiter = DELIMITERS.get(str(self.config.get("delimiter", "auto")))
        if delimiter:
            options["delimiter"] = delimiter
        try:
            return conn.read_csv(str(self._file()), **options)
        except duckdb.Error as exc:
            raise ConnectorError(f"Could not read the CSV file: {exc}") from exc

    def _csv_columns(self) -> list[ColumnSpec]:
        conn = duckdb.connect()
        try:
            return self._csv_types(self._csv_relation(conn))
        finally:
            conn.close()

    def _csv_types(self, relation: duckdb.DuckDBPyRelation) -> list[ColumnSpec]:
        columns = []
        for name, dtype in zip(relation.columns, relation.types, strict=True):
            kind = str(dtype).upper()
            if kind == "BOOLEAN":
                ctype = ColumnType.BOOLEAN
            elif kind == "DATE":
                ctype = ColumnType.DATE
            elif kind in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT"):
                ctype = ColumnType.INTEGER
            elif kind in ("DOUBLE", "FLOAT", "HUGEINT", "UBIGINT", "UINTEGER") or kind.startswith(
                "DECIMAL"
            ):
                ctype = ColumnType.FLOAT
            elif kind.startswith("TIMESTAMP"):
                ctype = ColumnType.TIMESTAMP
            else:
                ctype = ColumnType.STRING
            columns.append(ColumnSpec(name=name, type=ctype, source_type=kind.lower()))
        return columns

    def _read_csv(self, options: ReadOptions) -> Iterator[pa.RecordBatch]:
        casts = {
            ColumnType.BOOLEAN: "BOOLEAN",
            ColumnType.DATE: "DATE",
            ColumnType.INTEGER: "BIGINT",
            ColumnType.FLOAT: "DOUBLE",
            ColumnType.TIMESTAMP: "TIMESTAMP",
            ColumnType.STRING: "VARCHAR",
        }
        conn = duckdb.connect()
        try:
            relation = self._csv_relation(conn)
            columns = self._csv_types(relation)
            if options.columns:
                columns = [c for c in columns if c.name in options.columns]
            projection = ", ".join(
                'CAST("{0}" AS {1}) AS "{0}"'.format(c.name.replace('"', '""'), casts[c.type])
                for c in columns
            )
            schema = arrow_schema(columns)
            produced = False
            try:
                reader = relation.project(projection).to_arrow_reader(options.batch_size)
                for batch in reader:
                    produced = True
                    yield batch.cast(schema)
            except duckdb.Error as exc:
                raise ConnectorError(f"Could not read the CSV file: {exc}") from exc
            if not produced:
                yield rows_to_batch(columns, [])
        finally:
            conn.close()

    # ---- Excel -------------------------------------------------------------------

    def _sheets(self) -> dict[str, tuple[list[ColumnSpec], list[tuple[Any, ...]]]]:
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(self._file(), read_only=True, data_only=True)
        except Exception as exc:
            raise ConnectorError(f"Could not open the Excel file: {exc}") from exc
        sheets = {}
        try:
            for sheet in workbook.worksheets:
                rows = [
                    row
                    for row in sheet.iter_rows(values_only=True)
                    if any(cell is not None and cell != "" for cell in row)
                ]
                if not rows:
                    continue
                width = max(len(row) for row in rows)
                rows = [tuple(row) + (None,) * (width - len(row)) for row in rows]
                if self._header:
                    names, data = _unique_names(list(rows[0])), rows[1:]
                else:
                    names, data = [f"column_{i + 1}" for i in range(width)], rows
                columns = [
                    ColumnSpec(name=name, type=_infer([row[i] for row in data]))
                    for i, name in enumerate(names)
                ]
                # Mixed columns are stored as text; blank strings become nulls.
                cleaned = [tuple(None if cell == "" else cell for cell in row) for row in data]
                sheets[re.sub(r"\s+", " ", sheet.title).strip()] = (columns, cleaned)
        finally:
            workbook.close()
        if not sheets:
            raise ConnectorError("The workbook has no sheets with data.")
        return sheets

    # ---- Connector protocol ----------------------------------------------------

    def test_connection(self) -> TestResult:
        try:
            streams = self.discover()
        except ConnectorError as exc:
            return TestResult(ok=False, message=str(exc))
        columns = sum(len(s.columns) for s in streams)
        return TestResult(ok=True, message=f"Read {len(streams)} table(s), {columns} column(s).")

    def discover(self) -> list[StreamSpec]:
        if self._is_excel:
            return [
                StreamSpec(name=name, columns=tuple(columns), row_count_estimate=len(rows))
                for name, (columns, rows) in self._sheets().items()
            ]
        stem = re.sub(r"\s+", " ", self._file().stem).strip() or "data"
        return [StreamSpec(name=stem, columns=tuple(self._csv_columns()))]

    def read(
        self,
        stream: str,
        mode: RefreshMode,
        state: dict[str, Any] | None,
        options: ReadOptions | None = None,
    ) -> ReadResult:
        options = options or ReadOptions()
        if not self._is_excel:
            return ReadResult(self._read_csv(options))
        sheets = self._sheets()
        if stream not in sheets:
            raise ConnectorError(f"Sheet {stream!r} was not found in the workbook.")
        columns, rows = sheets[stream]
        if options.columns:
            keep = [i for i, c in enumerate(columns) if c.name in options.columns]
            columns = [columns[i] for i in keep]
            rows = [tuple(row[i] for i in keep) for row in rows]

        def batches() -> Iterator[pa.RecordBatch]:
            if not rows:
                yield rows_to_batch(columns, [])
            for start in range(0, len(rows), options.batch_size):
                yield rows_to_batch(columns, rows[start : start + options.batch_size])

        return ReadResult(batches())

    def close(self) -> None:
        return None


CONNECTOR = FileConnector
