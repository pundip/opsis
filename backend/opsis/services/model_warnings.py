"""Warnings about a semantic model: things that save, but may give numbers or
access that people do not expect. A warning never stops a save.

- A repeated join key. The compiler trusts a join declared many_to_one or
  one_to_one. If the joined dataset holds a key value on two rows, every row that
  matches it is counted twice. The current snapshot of each dataset a query can
  reach through such joins is checked. Nothing is stored: results are cached by
  snapshot, so the first look after a refresh checks the new data.
- Row-level security that does not carry over a join: a model with no rules of
  its own that joins to a dataset with rules. Those rules do not limit its rows.

Examples of repeated values are left out for people who may not see all of that
dataset: it is restricted to others, or row-level security applies to them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.errors import ApiError
from opsis.query.engine import CompiledQuery, QueryError
from opsis.semantic import model as semantic
from opsis.semantic.compiler import Source, repeated_keys
from opsis.services import access
from opsis.services import query as queries
from opsis.settings import Settings
from opsis.storage import snapshots

log = logging.getLogger("opsis.model_warnings")

MAX_EXAMPLES = 5
MAX_EXAMPLE_TEXT = 40  # characters of one example value in a message

# {"code", "path", "message", "dataset", "through"}; for a join key also "key", and
# for a repeated one "repeatedValues", "examples" and "examplesHidden".
ModelWarning = dict[str, Any]


def check(
    db: Session,
    settings: Settings,
    runtime: queries.QueryRuntime,
    principal: Principal,
    dataset: str,
    definition: Mapping[str, Any],
) -> list[ModelWarning]:
    """The warnings for this (valid) model of `dataset`, as `principal` may see
    them. Call it once anything is saved: it ends the database transaction before
    it reads any data."""
    base = semantic.parse_model(dataset, definition)
    if not base.joins:
        return []
    sources = queries.load_sources(db, settings, {join.to for join in base.joins})
    beyond = {join.to for source in sources.values() for join in source.model.joins}
    sources.update(queries.load_sources(db, settings, beyond - set(sources) - {dataset}))
    paths = semantic.join_paths(
        {**{name: source.model for name, source in sources.items()}, dataset: base},
        dataset,
        {name for name, source in sources.items() if source.restricted},
    )
    blocked = access.blocked_datasets(db, principal)
    user = queries.user_context(principal, None)
    db.rollback()  # nothing to hold open while the data is read

    found: list[ModelWarning] = []
    for path in paths:
        if path.join.relationship not in semantic.SAFE_JOINS:
            continue  # queries cannot use it
        source = sources[path.join.to]
        hidden = path.join.to in blocked or queries.rls_applies(source, user)
        warning = _repeated_key(settings, runtime, dataset, path, source, hidden)
        if warning is not None:
            found.append(warning)
    if not base.rls:
        found += [
            _rules_not_applied(dataset, path) for path in paths if sources[path.join.to].model.rls
        ]
    return found


def _repeated_key(
    settings: Settings,
    runtime: queries.QueryRuntime,
    base: str,
    path: semantic.JoinPath,
    source: Source,
    hidden: bool,
) -> ModelWarning | None:
    """A warning when the join's key is on more than one row of the dataset it
    reads, or when that could not be checked."""
    target = path.join.to
    key = list(dict.fromkeys(remote for _, remote in path.join.on))
    if any(column not in source.columns for column in key):
        return None  # a broken join, which Check reports as a problem
    about: ModelWarning = {
        "path": f"/joins/{path.index}",
        "dataset": target,
        "through": path.through,
        "key": key,
    }
    named = repr(key[0]) if len(key) == 1 else f"({', '.join(repr(c) for c in key)})"
    statement = repeated_keys(source, key, MAX_EXAMPLES)
    compiled = CompiledQuery(
        sql=statement.sql(dialect="duckdb", comments=False),
        params=(),
        tables=(source.table,),
        row_limit=MAX_EXAMPLES,
        timeout_s=settings.query_timeout_seconds,
    )
    try:
        table = runtime.execute(compiled, wait_seconds=settings.query_timeout_seconds).table
    except (QueryError, ApiError):  # too slow, too busy, or the data cannot be read
        log.warning("The join key of dataset %s could not be checked", target, exc_info=True)
        return {
            "code": "join_key_not_checked",
            **about,
            "message": f"Whether the key {named} of {target!r} is unique could not be checked "
            "just now (the server was busy, or reading the data failed or took too long). "
            "Check the model again.",
        }
    if table.num_rows == 0:
        return None
    data = [column.to_pylist() for column in table.columns]
    rows = [[column[i] for column in data] for i in range(table.num_rows)]
    repeated = int(rows[0][-1])
    # Each row: the key's values, the rows that hold them, and the number of such values.
    shown = [] if hidden else [(row[: len(key)], int(row[-2])) for row in rows]

    join = f"to {target!r}" if path.through is None else f"from {path.through!r} to {target!r}"
    values = "1 value is" if repeated == 1 else f"{repeated:,} values are"
    examples = ""
    if shown:
        listed = ", ".join(f"{_written(found)} ({count:,} rows)" for found, count in shown)
        examples = f", for example {listed}"
    withheld = (
        f" Examples are hidden: you may not see all of the data in {target!r}." if hidden else ""
    )
    meets = (
        "matches a repeated value"
        if path.through is None
        else f"leads through {path.through!r} to a repeated value"
    )
    elsewhere = "" if path.through is None else f" (the join is in the model of {path.through!r})"
    return {
        "code": "duplicate_join_key",
        **about,
        "repeatedValues": repeated,
        "examples": [
            {
                "values": {c: snapshots._jsonable(v) for c, v in zip(key, found, strict=True)},
                "rows": count,
            }
            for found, count in shown
        ],
        "examplesHidden": hidden,
        "message": f"The join {join} is declared {path.join.relationship}, but its key {named} "
        f"is not unique in {target!r}: {values} on more than one row{examples}.{withheld} "
        f"A row of {base!r} that {meets} is counted once for every row that holds it, so "
        f"totals and counts are too high in queries that use fields from {target!r}. Make "
        f"the key unique in {target!r}, or join on columns that pick out one row{elsewhere}.",
    }


def _written(values: list[Any]) -> str:
    """One repeated key value, or the values of a key with several columns."""

    def one(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            long = len(value) > MAX_EXAMPLE_TEXT
            return repr(f"{value[:MAX_EXAMPLE_TEXT]}..." if long else value)
        return str(snapshots._jsonable(value))

    if len(values) == 1:
        return one(values[0])
    return f"({', '.join(one(v) for v in values)})"


def _rules_not_applied(base: str, path: semantic.JoinPath) -> ModelWarning:
    target = path.join.to
    via = "" if path.through is None else f" (reached through {path.through!r})"
    return {
        "code": "joined_rules_not_applied",
        "path": f"/joins/{path.index}",
        "dataset": target,
        "through": path.through,
        "message": f"{target!r}{via} has row-level security but {base!r} has none, and rules on "
        f"a joined dataset do not limit the rows of {base!r}: a query that uses no field from "
        f"{target!r} returns all of them, even when the join is inner. To limit who sees which "
        f"rows of {base!r}, add a rule to its model on one of its own columns.",
    }
