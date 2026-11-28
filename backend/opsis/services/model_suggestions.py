"""Help with relationships (V2_DESIGN 1.3), for the Model view and the agent. Nothing
here changes the model: a person confirms a proposal, and it is saved as a patch.

- `check_pair`: what a relationship dragged from one field onto another would be: which
  side is the one side and the cardinality (from the keys' uniqueness, one query per
  side), a single filter direction, and active unless it would make two paths as
  short as each other or close a loop that row-level security would travel round.
- `suggest`: proposals for the model from names, types and value overlap, each with a
  confidence and the evidence, for example "98% of gifts.donor_id values exist in
  donors.id".

Values are read on the server and as the person: the overlap through row-level
security (`Security.scan`), from tables they may use. Uniqueness is counted over every
row, as the duplicate_key warning does, because the cardinality is the same for
everyone; only counts come back. Suggestions leave out restricted datasets, which no
path may go through.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.orm import Session
from sqlglot import exp

from opsis.auth.principal import Principal
from opsis.errors import ApiError
from opsis.query.engine import CompiledQuery, QueryError, TableRef
from opsis.semantic import workspace as ws
from opsis.semantic.relationships import (
    Path,
    Relationship,
    describe_path,
    grouping_steps,
    new_id,
    refused_loops,
    relationship_entry,
    shortest_paths,
)
from opsis.semantic.security import Source, UserContext, key_value, snapshot_scan
from opsis.semantic.workspace import Workspace
from opsis.services import query as queries
from opsis.services import workspace_model as wm
from opsis.settings import Settings

log = logging.getLogger("opsis.model")

SAMPLE = 10_000  # distinct values of the many side compared with the one side
MAX_PAIRS = 40  # column pairs read for one request, the likeliest by name first
MAX_PROPOSALS = 20
MAX_KEY_COLUMNS = 4
MIN_SHARE = 0.5  # a pair with fewer matching values is not proposed
KEY_TYPES = {"integer", "decimal", "string"}
DATE_TYPES = {"date", "timestamp"}
# Names too common to say that two columns hold the same thing.
GENERIC = {
    "id", "key", "code", "name", "label", "title", "description", "type", "kind", "status",
    "value", "amount", "total", "count", "number", "date", "notes", "created_at",
    "updated_at", "modified_at",
}  # fmt: skip
KEY_SUFFIXES = ("_id", "_code", "_key", "_no", "_number", "_ref")
NOTE = (
    "Nothing is saved until you confirm. Each proposal is a relationship in the form "
    "the workspace model stores (`relationship`), under the id `id`."
)


@dataclass(frozen=True)
class KeyStats:
    """A key's values over every row of its table."""

    rows: int
    keyed: int  # rows with a value in every key column
    distinct: int

    @property
    def unique(self) -> bool:
        return self.distinct == self.keyed

    def as_dict(self, table: str, columns: Sequence[str]) -> dict[str, Any]:
        return {
            "table": table,
            "columns": list(columns),
            "rows": self.rows,
            "emptyKeys": self.rows - self.keyed,
            "distinctKeys": self.distinct,
            "unique": self.unique,
        }


@dataclass(frozen=True)
class Overlap:
    sampled: int  # distinct values of the many side compared
    matched: int  # of those, the ones the one side has

    @property
    def share(self) -> float:
        return self.matched / self.sampled if self.sampled else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"sampled": self.sampled, "matched": self.matched, "share": round(self.share, 4)}


class Unreadable(Exception):
    """The data could not be read just now (busy, too slow, or a broken rule)."""


@dataclass
class _Context:
    settings: Settings
    runtime: queries.QueryRuntime
    workspace: Workspace
    user: UserContext
    allowed: wm.Visible
    taken: set[str]  # every relationship id in the stored model
    restricted: frozenset[str]
    date_tables: dict[str, str]  # a date table, and its date column
    primary_keys: dict[str, tuple[str, ...]]
    stats: dict[tuple[str, tuple[str, ...]], KeyStats] = field(default_factory=dict)

    @property
    def relationships(self) -> tuple[Relationship, ...]:
        """The relationships the person may see (both ends usable to them)."""
        return tuple(
            rel
            for rel in self.workspace.relationships
            if rel.from_table in self.allowed and rel.to_table in self.allowed
        )

    def source(self, name: str) -> Source:
        if name not in self.allowed or name not in self.workspace.sources:
            raise ApiError(404, "not_found", wm.NO_TABLE.format(name=name))
        return self.workspace.sources[name]


def _context(
    db: Session, settings: Settings, runtime: queries.QueryRuntime, principal: Principal
) -> _Context:
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    context = _Context(
        settings=settings,
        runtime=runtime,
        workspace=wm.workspace_of(settings, state),
        user=queries.user_context(principal, None),
        allowed=allowed,
        taken=set(state.stored["relationships"]),
        restricted=frozenset(n for n, d in state.datasets.items() if d.restricted),
        date_tables={
            name: entry["dateTable"]["column"]
            for name, entry in state.effective["tables"].items()
            if isinstance(entry.get("dateTable"), Mapping)
        },
        primary_keys={n: d.primary_key for n, d in state.datasets.items() if d.primary_key},
    )
    db.rollback()  # nothing to hold open while the data is read
    return context


# ---- reading the data -------------------------------------------------------------


def _run(context: _Context, select: exp.Select, tables: Sequence[TableRef]) -> list[Any]:
    """The one row a statement returns."""
    settings = context.settings
    compiled = CompiledQuery(
        sql=select.sql(dialect="duckdb", comments=False),
        params=(),
        tables=tuple(tables),
        row_limit=1,
        timeout_s=settings.query_timeout_seconds,
    )
    try:
        table = context.runtime.execute(compiled, wait_seconds=settings.query_timeout_seconds).table
    except (QueryError, ApiError) as exc:
        log.warning("Relationship data could not be read", exc_info=True)
        raise Unreadable from exc
    return [column[0].as_py() for column in table.columns]


def _present(columns: Sequence[str], table: str | None = None) -> exp.Expr:
    return exp.and_(
        *[
            exp.Not(
                this=exp.Is(this=exp.column(c, table=table, quoted=True), expression=exp.null())
            )
            for c in columns
        ]
    )


def key_stats(context: _Context, table: str, columns: Sequence[str]) -> KeyStats:
    """Rows, rows with a key and distinct keys of `columns`, over every row."""
    key = (table, tuple(columns))
    if key in context.stats:
        return context.stats[key]
    source = context.workspace.sources[table]
    present = _present(columns)
    distinct = (
        exp.select(*[exp.column(c, quoted=True) for c in columns])
        .distinct()
        .from_(snapshot_scan(source).subquery(exp.to_identifier("d", quoted=True)))
        .where(present.copy())
    )
    counted = exp.select(exp.Count(this=exp.Star())).from_(
        distinct.subquery(exp.to_identifier("u", quoted=True))
    )
    statement = exp.select(
        exp.alias_(exp.Count(this=exp.Star()), "rows", quoted=True),
        exp.alias_(
            exp.Filter(this=exp.Count(this=exp.Star()), expression=exp.Where(this=present)),
            "keyed",
            quoted=True,
        ),
        exp.alias_(counted.subquery(), "distinct", quoted=True),
    ).from_(snapshot_scan(source).subquery(exp.to_identifier("t", quoted=True)))
    rows, keyed, found = _run(context, statement, (source.table,))
    stats = KeyStats(int(rows or 0), int(keyed or 0), int(found or 0))
    context.stats[key] = stats
    return stats


def _keys(
    context: _Context,
    side: Source,
    columns: Sequence[str],
    other: Source,
    other_columns: Sequence[str],
    alias: str,
    as_text: bool,
) -> exp.Select:
    """The distinct key values of the rows of `side` the person may see."""
    scan, _ = context.workspace.security.scan(side, context.user)
    tz = context.workspace.timezone
    values = []
    for index, (column, opposite) in enumerate(zip(columns, other_columns, strict=True)):
        value = key_value(side, column, alias, other, opposite, tz)
        if as_text:
            value = exp.Cast(this=value, to=exp.DataType.build("VARCHAR"))
        values.append(exp.alias_(value, f"k{index}", quoted=True))
    return (
        exp.select(*values)
        .distinct()
        .from_(scan.subquery(exp.to_identifier(alias, quoted=True)))
        .where(_present(columns, alias))
    )


def overlap(
    context: _Context,
    many: str,
    many_columns: Sequence[str],
    one: str,
    one_columns: Sequence[str],
) -> Overlap:
    """How many of the many side's key values (up to SAMPLE of them) the one side has,
    both as the person sees them."""
    source, target = context.workspace.sources[many], context.workspace.sources[one]
    as_text = not all(
        ws.types_match(source.columns[a], target.columns[b])
        for a, b in zip(many_columns, one_columns, strict=True)
    )
    try:
        sampled = _keys(context, source, many_columns, target, one_columns, "m", as_text)
        found = _keys(context, target, one_columns, source, many_columns, "o", as_text)
    except ApiError as exc:  # a broken rule, or a loop that is refused
        raise Unreadable from exc
    match = exp.and_(
        *[
            exp.EQ(
                this=exp.column(f"k{i}", table="o", quoted=True),
                expression=exp.column(f"k{i}", table="f", quoted=True),
            )
            for i in range(len(many_columns))
        ]
    )
    statement = (
        exp.select(
            exp.alias_(exp.Count(this=exp.Star()), "sampled", quoted=True),
            exp.alias_(
                exp.Count(this=exp.column("k0", table="o", quoted=True)), "matched", quoted=True
            ),
        )
        .from_(sampled.limit(SAMPLE).subquery(exp.to_identifier("f", quoted=True)))
        .join(found.subquery(exp.to_identifier("o", quoted=True)), on=match, join_type="left")
    )
    tables = [source.table, target.table]
    sampled_count, matched = _run(context, statement, tables)
    return Overlap(int(sampled_count or 0), int(matched or 0))


# ---- the guesses ------------------------------------------------------------------


def _ambiguous(
    relationships: Sequence[Relationship], tables: Collection[str], blocked: Collection[str]
) -> dict[tuple[str, str], tuple[Path, Path]]:
    steps = grouping_steps(relationships)
    found: dict[tuple[str, str], tuple[Path, Path]] = {}
    for table in sorted(tables):
        for target, paths in shortest_paths(table, steps, blocked=blocked).ambiguous.items():
            found[(table, target)] = paths
    return found


def inactive_reason(
    context: _Context, relationships: Sequence[Relationship], new: Relationship
) -> str | None:
    """Why `new` should start inactive beside `relationships`: active, it would make two
    paths as short as each other, or close a loop that row-level security would travel
    round (which is refused)."""
    tables = context.workspace.sources
    active = replace(new, active=True)
    before = _ambiguous(relationships, tables, context.restricted)
    after = _ambiguous((*relationships, active), tables, context.restricted)
    for (start, target), (one, other) in sorted(after.items()):
        if (start, target) not in before:
            return (
                f"Active, it would give {start!r} two paths to {target!r} as short as each "
                f"other ({describe_path(one)}, and {describe_path(other)}), so it starts "
                "inactive. A measure can still use it by naming it in `relationship`."
            )
    models = context.workspace.models
    known = set(refused_loops(relationships, models))
    for loop in refused_loops((*relationships, active), models):
        if loop not in known:
            return (
                f"Active, it would make {' → '.join(loop)} a loop that row-level security "
                "would travel round, which is refused, so it starts inactive."
            )
    return None


def _confidence(name_score: float, share: float, unique: bool) -> float:
    found = 0.5 * name_score + 0.5 * share
    return round(found if unique else min(found, 0.3), 2)


def _percent(share: float) -> str:
    if 0.99 < share < 1:
        return "over 99%"
    if 0 < share < 0.01:
        return "under 1%"
    return f"{share:.0%}"


def _named(table: str, columns: Sequence[str]) -> str:
    if len(columns) == 1:
        return f"{table}.{columns[0]}"
    return f"{table} ({', '.join(columns)})"


def _overlap_evidence(
    found: Overlap, many: str, mc: Sequence[str], one: str, oc: Sequence[str]
) -> str:
    sampled = f" (of {found.sampled:,} values sampled)" if found.sampled >= SAMPLE else ""
    return (
        f"{_percent(found.share)} of {_named(many, mc)} values exist in {_named(one, oc)}{sampled}"
    )


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _table_words(table: str) -> set[str]:
    """How a column that points at `table` may name it: donors as donor or donors,
    demo_donors also as donor."""
    last = table.rsplit("_", 1)[-1]
    return {w for w in (table, _singular(table), last, _singular(last)) if w}


def name_score(
    context: _Context, many: str, column: str, one: str, key: str
) -> tuple[float, str] | None:
    """How strongly the names and types say that `many.column` holds keys of
    `one.key`, and why; None when they do not."""
    a = context.workspace.sources[many].columns.get(column)
    b = context.workspace.sources[one].columns.get(key)
    if a is None or b is None:
        return None
    if context.date_tables.get(one) == key:
        if a in DATE_TYPES and many not in context.date_tables:
            return 0.8, f"{one!r} is the date table, and {_named(many, [column])} is a {a}"
        return None
    if a not in KEY_TYPES or b not in KEY_TYPES or not ws.types_match(a, b):
        return None
    declared = context.primary_keys.get(one) == (key,)
    if column == key:
        if declared:
            return 0.9, f"{_named(many, [column])} has the name of {one}'s primary key"
        if key in GENERIC:
            return None
        if key.endswith(KEY_SUFFIXES):
            return 0.85, f"Both columns are called {key!r}"
        return 0.6, f"Both columns are called {key!r}"
    for word in sorted(_table_words(one)):
        if column in (f"{word}_{key}", f"{word}{key}") or (
            declared and column in (f"{word}_id", f"{word}_code") and key not in GENERIC
        ):
            return 0.9, f"{column!r} names the table {one!r} and its column {key!r}"
    return None


# ---- a dragged pair ---------------------------------------------------------------


def _side(context: _Context, given: Any, path: str) -> tuple[str, list[str]]:
    if not isinstance(given, Mapping) or not isinstance(given.get("table"), str):
        raise ApiError(422, "invalid_request", f"`{path}` needs a `table` and its `columns`.")
    columns = given.get("columns")
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= MAX_KEY_COLUMNS
        or not all(isinstance(c, str) for c in columns)
    ):
        raise ApiError(
            422,
            "invalid_request",
            f"`{path}.columns` needs 1 to {MAX_KEY_COLUMNS} column names.",
        )
    table = given["table"]
    source = context.source(table)
    for column in columns:
        if column not in source.columns:
            raise ApiError(
                422, "invalid_request", f"Table {table!r} has no column called {column!r}."
            )
    return table, list(columns)


def check_pair(
    db: Session,
    settings: Settings,
    runtime: queries.QueryRuntime,
    principal: Principal,
    dragged: Any,
    target: Any,
) -> dict[str, Any]:
    """The relationship a field dragged onto another would make, with its guessed
    settings and the evidence for them. Saves nothing."""
    context = _context(db, settings, runtime, principal)
    a_table, a_columns = _side(context, dragged, "from")
    b_table, b_columns = _side(context, target, "to")
    if a_table == b_table:
        raise ApiError(422, "invalid_request", "A relationship joins two different tables.")
    if len(a_columns) != len(b_columns):
        raise ApiError(422, "invalid_request", "Both sides of a relationship need as many columns.")
    warnings: list[dict[str, str]] = []
    evidence: list[str] = []
    try:
        a_stats = key_stats(context, a_table, a_columns)
        b_stats = key_stats(context, b_table, b_columns)
    except Unreadable:
        raise ApiError(
            503,
            "busy",
            "Whether the keys are unique could not be checked just now. Try again shortly.",
        ) from None
    swapped = a_stats.unique and not b_stats.unique
    many, many_columns, one, one_columns = (
        (b_table, b_columns, a_table, a_columns)
        if swapped
        else (a_table, a_columns, b_table, b_columns)
    )
    many_stats, one_stats = (b_stats, a_stats) if swapped else (a_stats, b_stats)
    if one_stats.unique and many_stats.unique:
        cardinality = "one_to_one"
        evidence.append(
            f"{_named(one, one_columns)} and {_named(many, many_columns)} are both unique"
        )
    else:
        cardinality = "many_to_one"
        if one_stats.unique:
            evidence.append(f"{_named(one, one_columns)} is unique")
        else:
            warnings.append(
                {
                    "code": "no_unique_side",
                    "message": f"Neither {_named(a_table, a_columns)} nor "
                    f"{_named(b_table, b_columns)} is unique, so this would be many-to-many, "
                    "which Opsis does not support yet: totals would count rows more than "
                    "once. Relate on columns that pick out one row on one side.",
                }
            )
    if swapped:
        evidence.append(
            f"{_named(a_table, a_columns)} is unique and {_named(b_table, b_columns)} is not, "
            f"so {b_table!r} is the many side"
        )
    sources = context.workspace.sources
    for local, remote in zip(many_columns, one_columns, strict=True):
        x, y = sources[many].columns[local], sources[one].columns[remote]
        if not ws.types_match(x, y):
            warnings.append(
                {
                    "code": "key_types_differ",
                    "message": f"{many}.{local} is {x} and {one}.{remote} is {y}. Values are "
                    "compared after converting one of them, which can fail or match less than "
                    "you expect.",
                }
            )
    for end in (one, many):
        if end in context.restricted:
            warnings.append(
                {
                    "code": "relationship_to_restricted",
                    "message": f"{end!r} is a restricted dataset. People who may use the other "
                    f"end but not {end!r} cannot use its fields through this relationship, "
                    "though its row-level security still filters what they see.",
                }
            )
            break
    found: Overlap | None = None
    try:
        found = overlap(context, many, many_columns, one, one_columns)
    except Unreadable:
        warnings.append(
            {
                "code": "overlap_not_checked",
                "message": "How many values match could not be checked just now.",
            }
        )
    if found is not None:
        if found.sampled:
            evidence.append(_overlap_evidence(found, many, many_columns, one, one_columns))
        else:
            warnings.append(
                {
                    "code": "no_values",
                    "message": f"{_named(many, many_columns)} has no values you may see.",
                }
            )
    named = name_score(context, many, many_columns[0], one, one_columns[0])
    if named is not None and len(many_columns) == 1:
        evidence.append(named[1])
    rel = Relationship(
        id=new_id(context.taken, many, one),
        from_table=many,
        from_columns=tuple(many_columns),
        to_table=one,
        to_columns=tuple(one_columns),
        cardinality=cardinality,
    )
    reason = inactive_reason(context, context.relationships, rel)
    if reason is not None:
        rel = replace(rel, active=False)
    result: dict[str, Any] = {
        "id": rel.id,
        "relationship": relationship_entry(rel),
        "swapped": swapped,
        "confidence": _confidence(
            named[0] if named else 0.5, found.share if found else 0.0, one_stats.unique
        ),
        "evidence": evidence,
        "warnings": warnings,
        "keys": {
            "from": many_stats.as_dict(many, many_columns),
            "to": one_stats.as_dict(one, one_columns),
        },
        "overlap": found.as_dict() if found is not None else None,
        "note": "Nothing is saved until you confirm.",
    }
    if reason is not None:
        result["inactiveBecause"] = reason
    return result


# ---- proposals for the model ------------------------------------------------------


@dataclass(frozen=True)
class _Pair:
    score: float
    why: str
    many: str
    column: str
    one: str
    key: str


def _related(relationships: Sequence[Relationship], pair: _Pair) -> bool:
    ends = {(pair.many, (pair.column,)), (pair.one, (pair.key,))}
    return any(
        {(r.from_table, r.from_columns), (r.to_table, r.to_columns)} == ends for r in relationships
    )


def suggest(
    db: Session,
    settings: Settings,
    runtime: queries.QueryRuntime,
    principal: Principal,
    tables: Collection[str] | None = None,
) -> dict[str, Any]:
    """Relationships the model does not have yet, from names, types and value overlap,
    with a confidence and the evidence (the most likely first). With `tables`, only
    those that touch one of them. Saves nothing."""
    context = _context(db, settings, runtime, principal)
    usable = sorted(
        name
        for name in context.workspace.sources
        if name in context.allowed and name not in context.restricted
    )
    wanted = set(usable)
    if tables is not None:
        for name in tables:
            context.source(name)  # a table that does not exist, or that they may not use
            if name in context.restricted:
                raise ApiError(
                    422,
                    "invalid_request",
                    f"{name!r} is a restricted dataset: suggestions leave those out, because "
                    "no path goes through one. Drag its fields in the Model view instead.",
                )
        wanted = set(tables)
    existing = context.relationships
    pairs: list[_Pair] = []
    for many in usable:
        for one in usable:
            if many == one or (many not in wanted and one not in wanted):
                continue
            for column in context.workspace.sources[many].columns:
                for key in context.workspace.sources[one].columns:
                    named = name_score(context, many, column, one, key)
                    if named is None:
                        continue
                    pair = _Pair(named[0], named[1], many, column, one, key)
                    if not _related(existing, pair):
                        pairs.append(pair)
    pairs.sort(key=lambda p: (-p.score, p.many, p.column, p.one, p.key))
    checked, not_checked = pairs[:MAX_PAIRS], len(pairs) - min(len(pairs), MAX_PAIRS)
    found: list[tuple[float, _Pair, Overlap, bool]] = []
    unreadable = 0
    for pair in checked:
        try:
            one_stats = key_stats(context, pair.one, [pair.key])
            if not one_stats.unique:
                continue
            shared = overlap(context, pair.many, [pair.column], pair.one, [pair.key])
            if not shared.sampled or shared.share < MIN_SHARE:
                continue
            many_unique = key_stats(context, pair.many, [pair.column]).unique
        except Unreadable:
            unreadable += 1
            continue
        found.append((_confidence(pair.score, shared.share, True), pair, shared, many_unique))
    found.sort(key=lambda f: (-f[0], f[1].many, f[1].column, f[1].one))
    proposals: list[dict[str, Any]] = []
    relationships = list(existing)
    taken = set(context.taken)
    seen: set[frozenset[tuple[str, str]]] = set()
    for confidence, pair, shared, many_unique in found:
        ends = frozenset({(pair.many, pair.column), (pair.one, pair.key)})
        if ends in seen:
            continue  # the same pair the other way round, both sides unique
        seen.add(ends)
        evidence = [
            _overlap_evidence(shared, pair.many, [pair.column], pair.one, [pair.key]),
            f"{pair.one}.{pair.key} is unique",
            pair.why,
        ]
        if many_unique:
            evidence.append(f"{pair.many}.{pair.column} is unique too, so it is one-to-one")
        rel = Relationship(
            id=new_id(taken, pair.many, pair.one),
            from_table=pair.many,
            from_columns=(pair.column,),
            to_table=pair.one,
            to_columns=(pair.key,),
            cardinality="one_to_one" if many_unique else "many_to_one",
        )
        # Judged with the proposals before it, as if they were all confirmed.
        reason = inactive_reason(context, relationships, rel)
        if reason is not None:
            rel = replace(rel, active=False)
        taken.add(rel.id)
        relationships.append(rel)
        proposal: dict[str, Any] = {
            "id": rel.id,
            "relationship": relationship_entry(rel),
            "confidence": confidence,
            "evidence": evidence,
        }
        if reason is not None:
            proposal["inactiveBecause"] = reason
        proposals.append(proposal)
        if len(proposals) >= MAX_PROPOSALS:
            break
    note = NOTE
    if not_checked:
        note += f" {not_checked} more column pairs were not read: name tables to narrow it."
    if unreadable:
        note += f" {unreadable} pairs could not be read just now: try again shortly."
    return {"proposals": proposals, "pairsChecked": len(checked), "note": note}
