"""Query engine interface (Milestone 0 stub: types and protocol only).

The engine executes already-compiled SQL against dataset snapshots. It does not
know about the semantic model, RLS or users: the compiler (opsis.semantic)
produces a CompiledQuery with RLS already applied, and the result cache wraps
the engine. This keeps the engine swappable (DuckDB now, something else later)
without touching the API.

Flow: semantic query -> compiler (sqlglot AST, RLS, row cap) -> CompiledQuery
      -> cache lookup -> QueryEngine.execute -> QueryResult
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from opsis.connectors.base import ColumnSpec, ColumnType

if TYPE_CHECKING:
    import pyarrow as pa


@dataclass(frozen=True)
class TableRef:
    """Binds a table alias used in the SQL to one immutable snapshot."""

    alias: str  # the name the compiled SQL uses, e.g. "gifts"
    dataset_id: str
    snapshot_id: str
    path: str  # directory of Parquet files for this snapshot


@dataclass(frozen=True)
class CompiledQuery:
    sql: str  # a single SELECT in the engine dialect, values as parameters
    params: tuple[Any, ...]
    tables: tuple[TableRef, ...]
    row_limit: int  # hard cap; the engine fetches row_limit + 1 to detect truncation
    timeout_s: float

    def cache_key(self, rls_context_hash: str) -> str:
        """Hash of (sql, params, snapshot ids, RLS context).

        A refresh changes the snapshot id, so stale entries are never hit.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class ResultColumn:
    name: str  # field reference, e.g. "gifts.total_amount"
    type: ColumnType
    role: str  # "dimension" | "measure" | "time"
    format: str | None = None  # measure format name, e.g. "currency_aud"


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[ResultColumn, ...]
    table: pa.Table
    row_count: int
    truncated: bool  # more rows existed than row_limit
    elapsed_ms: float
    cache_hit: bool = False


class QueryError(Exception):
    """Base class for engine failures."""


class QueryTimeoutError(QueryError):
    """The query exceeded CompiledQuery.timeout_s and was interrupted."""


@runtime_checkable
class QueryEngine(Protocol):
    dialect: ClassVar[str]  # sqlglot dialect the compiler must target, e.g. "duckdb"

    def execute(self, query: CompiledQuery) -> QueryResult:
        """Run the query read-only against the referenced snapshots.

        Must enforce row_limit and timeout_s, and must be safe to call from
        several threads at once.
        """
        ...

    def describe(self, table: TableRef) -> list[ColumnSpec]:
        """Column names and logical types of a snapshot."""
        ...

    def row_count(self, table: TableRef) -> int:
        """Exact row count of a snapshot (cheap: read from Parquet metadata)."""
        ...

    def close(self) -> None: ...
