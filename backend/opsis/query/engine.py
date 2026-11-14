"""Query engine interface.

The engine executes already-compiled SQL against dataset snapshots. It does not
know about the semantic model, RLS or users: the compiler (opsis.semantic)
produces a CompiledQuery with RLS already applied, and the result cache wraps
the engine. This keeps the engine swappable (DuckDB now, something else later)
without touching the API.

Flow: semantic query -> compiler (sqlglot AST, RLS, row cap) -> CompiledQuery
      -> cache lookup -> QueryEngine.execute -> QueryResult
"""

from __future__ import annotations

import hashlib
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
    sql: str  # a single SELECT in the engine dialect
    params: tuple[Any, ...]
    tables: tuple[TableRef, ...]
    row_limit: int  # the SQL asks for row_limit + 1 rows so truncation can be detected
    timeout_s: float
    # Set by the compiler when a row-level security rule existed but did not
    # apply to this user's role. Not used by the engine; shown to the user.
    rls_bypassed: bool = False

    def cache_key(self) -> str:
        """Identifies the result. The SQL already contains everything that can
        change it: the snapshot paths (a refresh makes a new one), the RLS
        values of the user, and relative dates resolved to real dates.
        """
        material = repr((self.sql, self.params, self.row_limit))
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class ResultColumn:
    name: str  # field reference, e.g. "gifts.total_amount"
    type: ColumnType
    role: str  # "dimension" | "measure" | "time"
    format: str | None = None  # measure format name, e.g. "currency_aud"
    label: str | None = None
    grain: str | None = None  # for the time column


@dataclass(frozen=True)
class QueryResult:
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
