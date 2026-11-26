"""Run a semantic query: validate, compile with RLS, execute through the cache."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from opsis.auth.principal import Permission, Principal, Role
from opsis.db.models import Dataset, SemanticModel, Snapshot, User
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
from opsis.semantic.compiler import Scan, Source, UserContext, compile_query, secured_scan
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
    if not names:
        return {}
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
            restricted=dataset.restricted,
        )
    return sources


def load_with_joins(
    db: Session, settings: Settings, names: set[str], base: str
) -> dict[str, Source]:
    """The named datasets, and the ones `base` joins to: a field two joins away is
    reached through one of those."""
    sources = load_sources(db, settings, names)
    if base in sources:
        hops = {join.to for join in sources[base].model.joins} - set(sources)
        sources.update(load_sources(db, settings, hops))
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


def user_context(
    principal: Principal, preview_as: Mapping[str, Any] | None, *, db: Session | None = None
) -> UserContext:
    """Who the query runs as, for row-level security.

    Admins may look as someone else ("View as"): a real user (`{"userId": ...}`),
    or a role and attribute set, to check rules before anyone relies on them.
    A share link never inherits an exemption, whatever its rules' role lists say.
    """
    if preview_as is None:
        return UserContext(
            principal.role.value,
            principal.email,
            dict(principal.attributes),
            may_be_exempt=principal.auth_method != "share_link",
        )
    if not principal.can(Permission.SETTINGS_MANAGE):
        raise ApiError(403, "forbidden", "Only an admin can view data as another user.")
    if preview_as.get("userId"):
        if db is None:
            raise ApiError(422, "invalid_request", "View as a user is not available here.")
        try:
            target = db.get(User, uuid.UUID(str(preview_as["userId"])))
        except ValueError:
            target = None
        if target is None:
            raise ApiError(404, "not_found", "The user to view as does not exist.")
        return UserContext(target.role, target.email, dict(target.attributes))
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


def rls_applies(source: Source, user: UserContext) -> bool:
    """True when a row-level security rule restricts what this user may see."""
    return any(user.role in rule.roles or not user.may_be_exempt for rule in source.model.rls)


def secured_select(source: Source, user: UserContext) -> exp.Subquery:
    """The dataset's rows as this user may see them, for use as a FROM clause."""
    scan, _ = secured_scan(source, user)
    return scan.subquery("t")


def run_raw(
    settings: Settings,
    runtime: QueryRuntime,
    source: Source,
    sql: str,
    limit: int,
    *,
    numbers: bool = False,
) -> list[list[Any]]:
    """Run SQL built over `secured_select` and return JSON-safe rows.
    With `numbers`, decimals become floats (for statistics) instead of text."""
    compiled = CompiledQuery(
        sql=sql,
        params=(),
        tables=(source.table,),
        row_limit=limit,
        timeout_s=settings.query_timeout_seconds,
    )
    try:
        table = runtime.execute(compiled, wait_seconds=settings.query_timeout_seconds).table
    except QueryTimeoutError as exc:
        raise ApiError(408, "query_timeout", str(exc)) from exc
    except QueryError as exc:
        raise ApiError(
            400, "query_failed", f"The rows could not be read: {str(exc).splitlines()[0][:300]}"
        ) from exc
    columns = [column.to_pylist() for column in table.columns]
    convert = _json if numbers else snapshots._jsonable
    return [[convert(column[i]) for column in columns] for i in range(table.num_rows)]


def secured_preview(
    settings: Settings, runtime: QueryRuntime, source: Source, user: UserContext, limit: int
) -> list[list[Any]]:
    """The first rows of a dataset with its row-level security applied."""
    sql = exp.select("*").from_(secured_select(source, user)).limit(limit).sql(dialect="duckdb")
    return run_raw(settings, runtime, source, sql, limit)


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
    check: Callable[[CompiledQuery], None] | None = None,
    scan: Scan | None = None,
) -> tuple[CompiledQuery, dict[str, Any]]:
    """Compile and run a schema-valid query against already loaded sources.
    `check` sees the compiled query before it runs, and may refuse it. `scan` reads
    each dataset for the user (the default applies its own rules, as v1 does)."""
    compiled, columns = compile_query(
        query,
        sources,
        user,
        settings.calendar,
        today or settings.today(),
        max_rows=min(max_rows or settings.query_max_rows, settings.query_max_rows),
        timeout_s=settings.query_timeout_seconds,
        scan=scan,
    )
    if check is not None:
        check(compiled)
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
    names = referenced_datasets(query)
    from opsis.services import access  # import here: access imports nothing from queries

    access.require_datasets(db, principal, names)
    base = str(query["dataset"])
    sources = load_with_joins(db, settings, names, base)
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
        user_context(principal, preview_as, db=db),
        query,
        max_rows=max_rows,
        today=today,
        # A dataset joined through is read too: the caller must be allowed to use it.
        check=lambda made: access.require_datasets(db, principal, {t.alias for t in made.tables}),
    )
    if include_sql and principal.can(Permission.MODELS_EDIT):
        body["sql"] = compiled.sql
    return body
