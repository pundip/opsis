"""Run a semantic query: validate, compile with RLS, execute through the cache."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Permission, Principal, Role
from opsis.db.models import Dataset, SemanticModel, Snapshot
from opsis.errors import ApiError
from opsis.query.duckdb_engine import ResultCache
from opsis.query.engine import (
    CompiledQuery,
    QueryEngine,
    QueryError,
    QueryResult,
    QueryTimeoutError,
    ResultColumn,
    TableRef,
)
from opsis.semantic import model as semantic
from opsis.semantic.compiler import Source, UserContext, compile_query
from opsis.services import models
from opsis.settings import Settings
from opsis.storage import snapshots


@dataclass
class QueryRuntime:
    """Per-process query machinery, created once at startup.

    Query endpoints are plain `def` functions, so FastAPI runs them in its
    worker thread pool and DuckDB never blocks the event loop. `slots` caps how
    many queries execute at once in this process; the rest wait their turn, so
    a burst of dashboard requests cannot multiply memory use.
    """

    engine: QueryEngine
    cache: ResultCache
    max_concurrent: int = 4
    slots: threading.BoundedSemaphore = field(init=False)

    def __post_init__(self) -> None:
        self.slots = threading.BoundedSemaphore(self.max_concurrent)

    def execute(self, compiled: CompiledQuery, wait_seconds: float) -> QueryResult:
        def run() -> QueryResult:
            if not self.slots.acquire(timeout=wait_seconds):
                raise ApiError(
                    503, "busy", "The server is busy running other queries. Try again shortly."
                )
            try:
                return self.engine.execute(compiled)
            finally:
                self.slots.release()

        return self.cache.get_or_run(compiled.cache_key(), run)


def load_sources(db: Session, settings: Settings, names: set[str]) -> dict[str, Source]:
    """The model, columns and current snapshot of each named dataset that has data."""
    rows = db.execute(
        select(Dataset, Snapshot, SemanticModel)
        .join(Snapshot, Snapshot.id == Dataset.current_snapshot_id)
        .outerjoin(SemanticModel, SemanticModel.dataset_id == Dataset.id)
        .where(Dataset.name.in_(names))
    ).all()
    sources = {}
    for dataset, snapshot, record in rows:
        path = snapshots.snapshot_dir(settings, dataset.id, snapshot.id)
        columns = {c["name"]: c["type"] for c in snapshot.columns}
        # A dataset refreshed before models existed has none stored: use the starter.
        definition = record.definition if record else models.starter(dataset, columns)
        sources[dataset.name] = Source(
            model=semantic.parse_model(dataset.name, definition),
            columns=columns,
            table=TableRef(dataset.name, str(dataset.id), str(snapshot.id), str(path)),
            timezones={c["name"]: c["tz"] for c in snapshot.columns if c.get("tz")},
        )
    return sources


def referenced_datasets(query: Mapping[str, Any]) -> set[str]:
    refs = [*(query.get("dimensions") or []), *(query.get("segments") or [])]
    refs += [f["field"] for f in query.get("filters") or []]
    if query.get("timeDimension"):
        refs.append(query["timeDimension"]["field"])
    return {str(query["dataset"]), *(str(r).partition(".")[0] for r in refs)}


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)  # charts need numbers; exact decimals stay in the snapshot
    return snapshots._jsonable(value)


def user_context(principal: Principal, preview_as: Mapping[str, Any] | None) -> UserContext:
    """Who the query runs as. Admins may preview as another role and attribute set,
    to check row-level security rules before anyone relies on them."""
    if preview_as is None:
        return UserContext(principal.role.value, principal.email, dict(principal.attributes))
    if not principal.can(Permission.SETTINGS_MANAGE):
        raise ApiError(403, "forbidden", "Only an admin can preview a query as another user.")
    try:
        role = Role(str(preview_as.get("role", "viewer")))
    except ValueError:
        raise ApiError(
            422, "invalid_request", "previewAs.role must be viewer, editor or admin."
        ) from None
    attributes = preview_as.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise ApiError(422, "invalid_request", "previewAs.attributes must be an object.")
    return UserContext(role.value, str(preview_as.get("email", "")), attributes)


def check_query(settings: Settings, query: Any) -> None:
    validator = semantic.schema_validator(settings.spec_dir, "semantic-query.schema.json")
    found = semantic.schema_problems(validator, query)
    if found:
        raise ApiError(422, "invalid_query", "The query is not valid.", found)


def column_dict(column: ResultColumn) -> dict[str, Any]:
    return {
        "name": column.name,
        "label": column.label,
        "type": column.type.value,
        "role": column.role,
        "format": column.format,
        "grain": column.grain,
    }


def execute(
    settings: Settings,
    runtime: QueryRuntime,
    sources: Mapping[str, Source],
    user: UserContext,
    query: Mapping[str, Any],
    *,
    max_rows: int | None = None,
    today: date | None = None,
) -> tuple[CompiledQuery, dict[str, Any]]:
    """Compile and run a schema-valid query against already loaded sources."""
    compiled, columns = compile_query(
        query,
        sources,
        user,
        settings.calendar,
        today or settings.today(),
        max_rows=min(max_rows or settings.query_max_rows, settings.query_max_rows),
        timeout_s=settings.query_timeout_seconds,
    )
    try:
        result = runtime.execute(compiled, wait_seconds=settings.query_timeout_seconds)
    except QueryTimeoutError as exc:
        raise ApiError(408, "query_timeout", str(exc)) from exc
    except QueryError as exc:
        raise ApiError(
            400, "query_failed", f"The query could not be run: {str(exc).splitlines()[0][:300]}"
        ) from exc

    data = [column.to_pylist() for column in result.table.columns]
    rows = [[_json(column[i]) for column in data] for i in range(result.row_count)]
    body: dict[str, Any] = {
        "columns": [column_dict(c) for c in columns],
        "rows": rows,
        "rowCount": result.row_count,
        "truncated": result.truncated,
        "elapsedMs": result.elapsed_ms,
        "cacheHit": result.cache_hit,
        # True when a row-level security rule exists on a dataset in this query
        # but does not apply to this user's role. The UI shows it as a badge.
        "rlsBypassed": compiled.rls_bypassed,
        "snapshots": {t.alias: t.snapshot_id for t in compiled.tables},
    }
    return compiled, body


def run(
    db: Session,
    settings: Settings,
    runtime: QueryRuntime,
    principal: Principal,
    query: Any,
    *,
    preview_as: Mapping[str, Any] | None = None,
    include_sql: bool = False,
    max_rows: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    check_query(settings, query)
    sources = load_sources(db, settings, referenced_datasets(query))
    base = str(query["dataset"])
    if base not in sources:
        exists = db.scalar(select(Dataset).where(Dataset.name == base))
        if exists is None:
            raise ApiError(404, "not_found", f"There is no dataset called {base!r}.")
        raise ApiError(
            409,
            "no_data",
            f"Dataset {base!r} has no data yet. Wait for its first refresh to finish.",
        )
    db.rollback()  # nothing to hold open while the query runs

    compiled, body = execute(
        settings,
        runtime,
        sources,
        user_context(principal, preview_as),
        query,
        max_rows=max_rows,
        today=today,
    )
    if include_sql and principal.can(Permission.MODELS_EDIT):
        body["sql"] = compiled.sql
    return body
