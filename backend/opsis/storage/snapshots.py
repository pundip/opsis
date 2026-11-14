"""Immutable Parquet snapshots.

Each refresh writes a new directory:

    <data_dir>/datasets/<dataset_id>/<snapshot_id>/part-00000.parquet ...

It is built under "<snapshot_id>.tmp" and renamed when complete, and Postgres
points to the current one. Readers therefore never see partial data, and a
failed refresh leaves the previous snapshot untouched.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from opsis.connectors.base import RefreshMode
from opsis.connectors.tabular import column_type_of
from opsis.settings import Settings

ROWS_PER_FILE = 1_000_000
SEQ = "_opsis_seq"  # ordering column used only while merging


class SnapshotError(Exception):
    """Raised for problems the user can act on. The message is shown in the job."""


@dataclass(frozen=True)
class BuiltSnapshot:
    snapshot_id: uuid.UUID
    path: Path
    row_count: int
    size_bytes: int
    columns: list[dict[str, str]]


def dataset_dir(settings: Settings, dataset_id: uuid.UUID) -> Path:
    return settings.data_dir / "datasets" / str(dataset_id)


def snapshot_dir(settings: Settings, dataset_id: uuid.UUID, snapshot_id: uuid.UUID) -> Path:
    return dataset_dir(settings, dataset_id) / str(snapshot_id)


def parts(path: Path) -> list[Path]:
    return sorted(path.glob("part-*.parquet"))


def _sql_str(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _PartWriter:
    """Writes record batches to numbered Parquet files, rolling over by row count."""

    def __init__(self, folder: Path, start_index: int = 0) -> None:
        self.folder = folder
        self.index = start_index
        self.rows = 0
        self.schema: pa.Schema | None = None
        self._writer: pq.ParquetWriter | None = None
        self._rows_in_file = 0

    def write(self, batch: pa.RecordBatch) -> None:
        if self.schema is None:
            self.schema = batch.schema
        elif batch.schema != self.schema:
            batch = batch.cast(self.schema)
        if self._writer is None:
            path = self.folder / f"part-{self.index:05d}.parquet"
            self._writer = pq.ParquetWriter(path, self.schema, compression="zstd")
        self._writer.write_batch(batch)
        self.rows += batch.num_rows
        self._rows_in_file += batch.num_rows
        if self._rows_in_file >= ROWS_PER_FILE:
            self._roll()

    def _roll(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self.index += 1
            self._rows_in_file = 0

    def close(self) -> None:
        self._roll()


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)  # same volume: instant, no extra disk
    except OSError:
        shutil.copy2(source, target)


def _same_shape(a: pa.Schema, b: pa.Schema) -> bool:
    return a.names == b.names and [f.type for f in a] == [f.type for f in b]


def _with_seq(batch: pa.RecordBatch, start: int) -> pa.RecordBatch:
    seq = pa.array(range(start, start + batch.num_rows), type=pa.int64())
    return pa.RecordBatch.from_arrays(
        [*batch.columns, seq], schema=batch.schema.append(pa.field(SEQ, pa.int64()))
    )


def _merge(previous: Path, incoming: Path, target: Path, primary_key: tuple[str, ...]) -> None:
    """Old rows plus new rows, keeping the newest row for each primary key."""
    keys = ", ".join(_ident(k) for k in primary_key)
    old = _sql_str(previous / "part-*.parquet")
    new = _sql_str(incoming / "part-*.parquet")
    conn = duckdb.connect()
    try:
        conn.execute("SET memory_limit = '1GB'")
        conn.execute(f"SET temp_directory = {_sql_str(target / '_duck')}")
        conn.execute("SET preserve_insertion_order = false")
        conn.execute(
            f"""
            COPY (
              SELECT * EXCLUDE (_src, {SEQ}, _rn) FROM (
                SELECT *, row_number() OVER (
                         PARTITION BY {keys} ORDER BY _src DESC, {SEQ} DESC) AS _rn
                FROM (
                  SELECT *, 0 AS _src, 0 AS {SEQ} FROM read_parquet({old})
                  UNION ALL BY NAME
                  SELECT *, 1 AS _src FROM read_parquet({new})
                )
              ) WHERE _rn = 1
            ) TO {_sql_str(target / "part-00000.parquet")} (FORMAT parquet, COMPRESSION zstd)
            """
        )
    finally:
        conn.close()
        shutil.rmtree(target / "_duck", ignore_errors=True)


def build_snapshot(
    settings: Settings,
    dataset_id: uuid.UUID,
    batches: Iterator[pa.RecordBatch],
    *,
    mode: RefreshMode,
    previous: Path | None,
    primary_key: tuple[str, ...] = (),
) -> BuiltSnapshot:
    """Write a new snapshot and return its details. Nothing is visible until it is complete.

    FULL (or no previous snapshot): the new rows are the snapshot.
    APPEND: previous files are hard-linked, new rows are added as extra files.
    MERGE: previous and new rows are combined, newest row per primary key wins.
    """
    snapshot_id = uuid.uuid4()
    final = snapshot_dir(settings, dataset_id, snapshot_id)
    tmp = final.with_name(final.name + ".tmp")
    tmp.mkdir(parents=True, exist_ok=False)
    incremental = previous is not None and previous.is_dir() and bool(parts(previous))
    if not incremental:
        mode = RefreshMode.FULL
    batches = _with_distinct_names(batches)
    try:
        if mode == RefreshMode.MERGE:
            assert previous is not None
            if not primary_key:
                raise SnapshotError("Merge refresh needs a primary key.")
            incoming = tmp / "_incoming"
            incoming.mkdir()
            writer = _PartWriter(incoming)
            for batch in batches:
                writer.write(_with_seq(batch, writer.rows))
            writer.close()
            previous_schema = pq.read_schema(parts(previous)[0])
            if writer.schema is None:
                raise SnapshotError("The source returned no data and no schema.")
            if not _same_shape(writer.schema.remove(len(writer.schema) - 1), previous_schema):
                raise SnapshotError(_schema_changed())
            if writer.rows:
                _merge(previous, incoming, tmp, primary_key)
            else:
                for part in parts(previous):
                    _link_or_copy(part, tmp / part.name)
            shutil.rmtree(incoming)
        else:
            start = 0
            previous_schema = None
            if mode == RefreshMode.APPEND:
                assert previous is not None
                for part in parts(previous):
                    _link_or_copy(part, tmp / part.name)
                start = len(parts(previous))
                previous_schema = pq.read_schema(parts(previous)[0])
            writer = _PartWriter(tmp, start_index=start)
            for batch in batches:
                if previous_schema is not None:
                    if not _same_shape(batch.schema, previous_schema):
                        raise SnapshotError(_schema_changed())
                    if batch.num_rows == 0:
                        continue  # nothing new: the linked files are the snapshot
                writer.write(batch)  # FULL writes even an empty batch, to keep the schema
            writer.close()
            if previous_schema is None and writer.schema is None:
                raise SnapshotError("The source returned no data and no schema.")

        files = parts(tmp)
        if not files:
            raise SnapshotError("The refresh produced no data files.")
        schema = pq.read_schema(files[0])
        row_count = sum(pq.read_metadata(f).num_rows for f in files)
        size = sum(f.stat().st_size for f in files)
        columns = [column_info(f) for f in schema]
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return BuiltSnapshot(snapshot_id, final, row_count, size, columns)


def column_info(field: pa.Field) -> dict[str, str]:
    """How a stored column is described: {"name", "type"} plus "tz" for instants."""
    info = {"name": field.name, "type": column_type_of(field.type).value}
    if pa.types.is_timestamp(field.type) and field.type.tz:
        info["tz"] = "UTC"  # timezone-aware values are always stored in UTC
    return info


def distinct_names(names: list[str]) -> list[str]:
    """Column names that differ by more than letter case.

    The query engine treats "Fund" and "fund" as the same column, so the second
    one is stored as "fund_2". Sources rarely have such columns, but a
    spreadsheet can.
    """
    seen: set[str] = set()
    result = []
    for name in names:
        candidate, n = name, 2
        while candidate.lower() in seen:
            candidate, n = f"{name}_{n}", n + 1
        seen.add(candidate.lower())
        result.append(candidate)
    return result


def _with_distinct_names(batches: Iterator[pa.RecordBatch]) -> Iterator[pa.RecordBatch]:
    for batch in batches:
        names = distinct_names(batch.schema.names)
        yield batch if names == batch.schema.names else batch.rename_columns(names)


def _schema_changed() -> str:
    return (
        "The columns in the source have changed since the last refresh. "
        "Run a full refresh to rebuild the dataset."
    )


def remove_snapshot(settings: Settings, dataset_id: uuid.UUID, snapshot_id: uuid.UUID) -> None:
    shutil.rmtree(snapshot_dir(settings, dataset_id, snapshot_id), ignore_errors=True)


def remove_dataset(settings: Settings, dataset_id: uuid.UUID) -> None:
    shutil.rmtree(dataset_dir(settings, dataset_id), ignore_errors=True)


def sweep_temp(settings: Settings) -> None:
    """Delete half-built snapshots left behind by a crash. Called at runner start."""
    root = settings.data_dir / "datasets"
    if root.is_dir():
        for leftover in root.glob("*/*.tmp"):
            shutil.rmtree(leftover, ignore_errors=True)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def preview(path: Path, limit: int) -> list[list[Any]]:
    """The first `limit` rows of a snapshot, as JSON-safe values."""
    rows: list[list[Any]] = []
    for part in parts(path):
        file = pq.ParquetFile(part)
        for batch in file.iter_batches(batch_size=min(limit, 5000)):
            columns = [column.to_pylist() for column in batch.columns]
            for i in range(batch.num_rows):
                rows.append([_jsonable(column[i]) for column in columns])
                if len(rows) >= limit:
                    return rows
    return rows
