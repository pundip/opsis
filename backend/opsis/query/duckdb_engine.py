"""DuckDB implementation of QueryEngine, plus the in-process result cache.

Each query gets its own short-lived in-memory DuckDB connection that reads the
snapshot's Parquet files. Nothing is written, so there is no lock to contend
for and any number of queries can run side by side.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import duckdb
import pyarrow.parquet as pq

from opsis.connectors.base import ColumnSpec
from opsis.connectors.tabular import column_type_of
from opsis.query.engine import CompiledQuery, QueryError, QueryResult, QueryTimeoutError, TableRef


class DuckDBEngine:
    dialect: ClassVar[str] = "duckdb"

    def __init__(
        self, *, memory_limit_mb: int = 1024, threads: int = 4, temp_dir: Path | None = None
    ) -> None:
        self._memory_limit_mb = memory_limit_mb
        self._threads = threads
        # DuckDB spills large sorts and joins to disk; its default (".tmp" in the
        # working directory) is not writable in the container.
        self._temp_dir = temp_dir or Path(tempfile.gettempdir()) / "opsis-duckdb"

    def _connect(self) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(":memory:")
        conn.execute(f"SET memory_limit = '{int(self._memory_limit_mb)}MB'")
        conn.execute(f"SET threads = {int(self._threads)}")
        if self._temp_dir is not None:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
            conn.execute("SET temp_directory = '" + str(self._temp_dir).replace("'", "''") + "'")
        # Queries only read local Parquet. Nothing may be fetched or installed.
        conn.execute("SET autoinstall_known_extensions = false")
        conn.execute("SET autoload_known_extensions = false")
        return conn

    def execute(self, query: CompiledQuery) -> QueryResult:
        started = time.monotonic()
        conn = self._connect()
        timed_out = threading.Event()

        def stop() -> None:
            timed_out.set()
            conn.interrupt()

        timer = threading.Timer(query.timeout_s, stop)
        timer.start()
        try:
            table = conn.sql(query.sql).to_arrow_table()
        except duckdb.Error as exc:
            if timed_out.is_set():
                raise QueryTimeoutError(
                    f"The query took longer than {query.timeout_s:g} seconds and was stopped."
                ) from None
            raise QueryError(str(exc)) from exc
        finally:
            timer.cancel()
            conn.close()
        truncated = table.num_rows > query.row_limit
        if truncated:
            table = table.slice(0, query.row_limit)
        elapsed = (time.monotonic() - started) * 1000
        return QueryResult(table, table.num_rows, truncated, round(elapsed, 1))

    def describe(self, table: TableRef) -> list[ColumnSpec]:
        first = sorted(Path(table.path).glob("part-*.parquet"))[0]
        return [ColumnSpec(f.name, column_type_of(f.type)) for f in pq.read_schema(first)]

    def row_count(self, table: TableRef) -> int:
        files = sorted(Path(table.path).glob("part-*.parquet"))
        return sum(pq.read_metadata(f).num_rows for f in files)

    def close(self) -> None:
        return None


class ResultCache:
    """A small thread-safe LRU of query results, held in this process.

    Entries never need explicit invalidation: the key is derived from the SQL,
    which names the snapshot directory, so a refresh simply stops hitting the
    old entries and they age out.
    """

    def __init__(self, max_entries: int = 200) -> None:
        self._max = max_entries
        self._items: OrderedDict[str, QueryResult] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get_or_run(self, key: str, run: Callable[[], QueryResult]) -> QueryResult:
        with self._lock:
            cached = self._items.get(key)
            if cached is not None:
                self._items.move_to_end(key)
                self.hits += 1
                return replace(cached, cache_hit=True, elapsed_ms=0.0)
            self.misses += 1
        result = run()  # outside the lock: slow queries must not block others
        if self._max > 0:
            with self._lock:
                self._items[key] = result
                self._items.move_to_end(key)
                while len(self._items) > self._max:
                    self._items.popitem(last=False)
        return result

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
