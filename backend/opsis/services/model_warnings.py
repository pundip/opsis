"""Warnings about the workspace model: things that save, but may give numbers or
access that people do not expect. A warning never stops a save (V2_DESIGN 1.2).

Per relationship:

- **A repeated key on the one side.** A query trusts a relationship to find one row
  on its one side. If that table holds a key value on two rows, every row that
  matches it is counted twice. The current snapshot is checked; nothing is stored:
  results are cached by snapshot, so the first look after a refresh checks the new
  data. Examples are left out for people who may not see all of that table.
- **Key columns of different types, two paths as short as each other, and a
  relationship into a restricted table**, found without data
  (`workspace.structure_warnings`).

The v1 view of one table (`for_table`) shows its joins' warnings at `/joins/<n>`.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import Any

from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import Dataset
from opsis.errors import ApiError
from opsis.query.engine import CompiledQuery, QueryError
from opsis.semantic import workspace as ws
from opsis.semantic.compiler import Source, UserContext, repeated_keys
from opsis.semantic.relationships import Relationship
from opsis.semantic.workspace import Workspace
from opsis.services import query as queries
from opsis.services import workspace_model as wm
from opsis.settings import Settings
from opsis.storage import snapshots

log = logging.getLogger("opsis.model_warnings")

MAX_EXAMPLES = 5
MAX_EXAMPLE_TEXT = 40  # characters of one example value in a message
V1_CODES = {"duplicate_key": "duplicate_join_key", "key_not_checked": "join_key_not_checked"}

# {"code", "path", "message"}; for a key, also "relationship", "dataset" and "key", and
# for a repeated one "repeatedValues", "examples" and "examplesHidden".
ModelWarning = dict[str, Any]


def check(
    db: Session,
    settings: Settings,
    runtime: queries.QueryRuntime,
    principal: Principal,
    document: Mapping[str, Any] | None = None,
    *,
    only: Collection[str] | None = None,
) -> list[ModelWarning]:
    """The warnings for the workspace model, or for `document` (a whole valid model
    about to be saved), as `principal` may see them. `only` limits them to some
    relationships. It ends the database transaction before it reads any data."""
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    stored = state.stored if document is None else ws.definition_of(document)
    full, _ = ws.effective(stored, state.datasets)
    workspace = wm.workspace_of(settings, state, full)
    user = queries.user_context(principal, None)
    db.rollback()  # nothing to hold open while the data is read

    def shown(rel_id: str | None) -> bool:
        return only is None or rel_id in only

    found: list[ModelWarning] = []
    for warning in ws.structure_warnings(stored, state.datasets):
        rid = warning["path"].split("/")[2]
        if shown(rid) and not wm.warning_hidden(warning, allowed, full):
            found.append(warning)
    for rel in workspace.relationships:
        if not shown(rel.id) or rel.from_table not in allowed or rel.to_table not in allowed:
            continue
        repeated = _repeated_key(settings, runtime, workspace, rel, user)
        if repeated is not None:
            found.append(repeated)
    return found


def for_table(warnings: list[ModelWarning], positions: Mapping[str, str]) -> list[ModelWarning]:
    """The warnings of one table's joins, as its v1 model shows them: `positions`
    maps each relationship id to its join's index."""
    result = []
    for warning in warnings:
        rid = warning["path"].split("/")[2]
        if rid not in positions:
            continue
        result.append(
            {
                **warning,
                "code": V1_CODES.get(warning["code"], warning["code"]),
                "path": f"/joins/{positions[rid]}",
            }
        )
    return result


def check_table(
    db: Session,
    settings: Settings,
    runtime: queries.QueryRuntime,
    principal: Principal,
    dataset: Dataset,
    definition: Mapping[str, Any] | None = None,
) -> list[ModelWarning]:
    """The warnings of one table's joins as its v1 model shows them: for the stored
    model, or for `definition`, a valid v1 model of the table about to be saved."""
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    visible = ws.visible_part(state.effective, allowed)
    document = None
    if definition is None:
        positions = ws.view_positions(visible, dataset.name)
    else:
        edited, by_index = ws.apply_table_view(visible, dataset.name, definition)
        positions = {rid: index for index, rid in by_index.items()}
        document = ws.merge_visible(state.effective, edited, allowed)
    found = check(db, settings, runtime, principal, document, only=set(positions))
    return for_table(found, positions)


def _examples_hidden(workspace: Workspace, source: Source, user: UserContext) -> bool:
    """True when this person may not see every row of the table."""
    try:
        return workspace.security.restricts(source, user)
    except ApiError:  # a broken rule or a refused loop: show nothing of it
        return True


def _repeated_key(
    settings: Settings,
    runtime: queries.QueryRuntime,
    workspace: Workspace,
    rel: Relationship,
    user: UserContext,
) -> ModelWarning | None:
    """A warning when the relationship's key is on more than one row of its one
    side, or when that could not be checked."""
    target = rel.to_table
    source = workspace.sources[target]
    key = list(dict.fromkeys(rel.to_columns))
    if any(column not in source.columns for column in key):
        return None  # a broken relationship, which Check reports as a problem
    about: ModelWarning = {
        "path": f"/relationships/{rel.id}",
        "relationship": rel.id,
        "dataset": target,
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
        log.warning("The key of relationship %s could not be checked", rel.id, exc_info=True)
        return {
            "code": "key_not_checked",
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
    hidden = _examples_hidden(workspace, source, user)
    # Each row: the key's values, the rows that hold them, and the number of such values.
    shown = [] if hidden else [(row[: len(key)], int(row[-2])) for row in rows]

    values = "1 value is" if repeated == 1 else f"{repeated:,} values are"
    examples = ""
    if shown:
        listed = ", ".join(f"{_written(found)} ({count:,} rows)" for found, count in shown)
        examples = f", for example {listed}"
    withheld = (
        f" Examples are hidden: you may not see all of the data in {target!r}." if hidden else ""
    )
    return {
        "code": "duplicate_key",
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
        "message": f"The relationship {rel.describe()} is {rel.cardinality.replace('_', '-')}, "
        f"but its key {named} is not unique in {target!r}: {values} on more than one "
        f"row{examples}.{withheld} A row of {rel.from_table!r} that matches a repeated value "
        f"is counted once for every row that holds it, so totals and counts are too high in "
        f"queries that use fields from {target!r}. Make the key unique in {target!r}, or "
        "relate on columns that pick out one row.",
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
