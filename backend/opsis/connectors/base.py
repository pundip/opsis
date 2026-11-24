"""Connector plugin interface (Milestone 0 stub: types and protocol only).

A connector pulls data from one source system as Arrow record batches. It knows
nothing about Parquet, snapshots or Postgres: the storage layer owns those.

Built-in connectors live in backend/opsis/connectors/<id>/. Third-party ones go
in plugins/connectors/<id>/ or register under the "opsis.connectors" entry
point. The registry (M2) discovers all three, so adding a connector never
touches core code.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pyarrow as pa


class ColumnType(StrEnum):
    """Logical column types shared by connectors, storage and the semantic model."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    JSON = "json"


class RefreshMode(StrEnum):
    FULL = "full"  # replace everything
    APPEND = "append"  # incremental, rows where cursor > last cursor value
    MERGE = "merge"  # incremental, de-duplicated by primary key at snapshot build


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # stop pytest collecting this as a test class

    ok: bool
    message: str = ""  # safe to show to the user; must never contain secrets
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: ColumnType
    nullable: bool = True
    source_type: str | None = None  # native type name, for display only
    scale: int | None = None  # digits after the point, for DECIMAL columns
    # TIMESTAMP columns only: the source values identify an instant (for example
    # Postgres timestamptz). They are stored in UTC and converted to the
    # workspace timezone when queried. False means wall-clock time as written.
    tz_aware: bool = False


@dataclass(frozen=True)
class StreamSpec:
    """One table or object that can become a dataset."""

    name: str  # stable identifier passed back to read(), e.g. "sales.orders"
    columns: tuple[ColumnSpec, ...]
    label: str | None = None
    primary_key: tuple[str, ...] = ()
    cursor_candidates: tuple[str, ...] = ()  # columns usable as incremental cursors
    supported_modes: tuple[RefreshMode, ...] = (RefreshMode.FULL,)
    row_count_estimate: int | None = None


@dataclass(frozen=True)
class ReadOptions:
    """Per-dataset read settings chosen by the user when creating the dataset."""

    columns: tuple[str, ...] | None = None  # None means all columns
    cursor_field: str | None = None  # required for APPEND and MERGE
    primary_key: tuple[str, ...] = ()  # required for MERGE
    batch_size: int = 50_000


class ReadResult:
    """A lazy stream of record batches plus the cursor state to store afterwards.

    The caller must fully consume `batches` before calling `final_state()`.
    The state is an opaque JSON-serialisable dict owned by the connector, for
    example {"cursor": "2026-01-31T00:00:00Z"}. It is persisted only when the
    new snapshot has been swapped in, so a failed refresh never advances it.
    """

    def __init__(
        self,
        batches: Iterator[pa.RecordBatch],
        final_state: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.batches = batches
        self._final_state = final_state

    def final_state(self) -> dict[str, Any]:
        return self._final_state() if self._final_state else {}


class ConnectorError(Exception):
    """Raised for source failures. The message is shown to users: no secrets."""


class ReconnectNeeded(ConnectorError):
    """The source no longer accepts the sign-in saved with the connection (for
    example Google answers invalid_grant): a person has to sign in again."""


@runtime_checkable
class Connector(Protocol):
    id: ClassVar[str]  # "mysql"
    name: ClassVar[str]  # "MySQL"
    version: ClassVar[str]  # "1.0.0"

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        """JSON Schema for the connection form.

        Secret fields are marked with "x-secret": true. They are encrypted at
        rest (Fernet), never returned by the API and never logged.
        """
        ...

    def __init__(self, config: dict[str, Any]) -> None:
        """Receives the validated config with secrets already decrypted.

        Must not open network connections here: do that lazily. The host adds
        one reserved key, "_uploads_dir": the directory that holds uploaded
        files, for connectors whose schema has fields marked "x-upload": true
        (their value is a path relative to that directory).
        """
        ...

    def test_connection(self) -> TestResult:
        """Cheap connectivity and credentials check. Must not raise."""
        ...

    def discover(self) -> list[StreamSpec]:
        """List tables or objects with their columns and types."""
        ...

    def read(
        self,
        stream: str,
        mode: RefreshMode,
        state: dict[str, Any] | None,
        options: ReadOptions | None = None,
    ) -> ReadResult:
        """Read one stream.

        `state` is the value returned by final_state() on the previous
        successful refresh, or None on the first run or after a full reset.
        In FULL mode the connector ignores `state`. The result must contain at
        least one batch, even an empty one, so the schema is always known.
        """
        ...

    def close(self) -> None:
        """Release connections. Called once after the refresh or test finishes."""
        ...
