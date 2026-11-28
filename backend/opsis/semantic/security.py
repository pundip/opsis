"""How a person's rows of each table are read: row-level security (ADR-032).

Every read of a table on someone's behalf (queries, previews, profiles, filter
options, samples for the agent) goes through `Security.scan`, so nothing can show a
row a rule hides:

- **R1, a table's own rules:** the rules that apply to the person's role are ANDed; a
  missing attribute denies; a broken rule blocks the table; admins are exempt unless
  listed (the badge); share links are never exempt.
- **R2, through relationships:** for each active relationship from T (many) to S
  (one) where S's rows are restricted for this person, T keeps the rows whose key
  matches one of S's visible rows, so a row with an empty or unmatched key is hidden.
  With `securityFilter: both`, S likewise keeps only rows related to T's visible
  rows. It is transitive, and it never comes back along the way it went. A table read
  only to filter another is not a use of it: no access check, and none of its rows
  are returned. A loop of active relationships that a rule could travel round is
  refused (`relationships.refused_loops`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from opsis.errors import ApiError
from opsis.query.engine import TableRef
from opsis.semantic import expressions
from opsis.semantic.expressions import ExpressionError
from opsis.semantic.model import Model
from opsis.semantic.relationships import (
    LoopError,
    Relationship,
    active_edges,
    reachable,
    refused_loops,
)

DIALECT = "duckdb"


@dataclass(frozen=True)
class Source:
    """One table as the compiler sees it: its model, columns and current snapshot."""

    model: Model
    columns: Mapping[str, str]  # column name -> logical type
    table: TableRef
    # Timestamp columns that hold instants, and the zone they are stored in (UTC).
    timezones: Mapping[str, str] = field(default_factory=dict)
    # A restricted dataset (an access list): a path may not go through it.
    restricted: bool = False


@dataclass(frozen=True)
class UserContext:
    role: str
    email: str = ""
    attributes: Mapping[str, Any] | None = None
    # False for share links and any anonymous access: every RLS rule then
    # applies whatever its role list says, even if the link's owner is an admin.
    may_be_exempt: bool = True

    def values(self) -> dict[str, Any]:
        values: dict[str, Any] = {"role": self.role, "email": self.email}
        for name, value in (self.attributes or {}).items():
            values[f"attributes.{name}"] = value
        return values


# How a table's rows are read for a person: `SELECT * FROM <snapshot> WHERE <security>`,
# and whether a rule was passed over (the badge).
Scan = Callable[[Source, UserContext], tuple[exp.Select, bool]]


def snapshot_scan(source: Source) -> exp.Select:
    """`SELECT * FROM <snapshot>`: every row, with no row-level security."""
    scan = sqlglot.parse_one("SELECT * FROM read_parquet('x')", read=DIALECT)
    literal, table = scan.find(exp.Literal), scan.find(exp.Table)
    assert isinstance(scan, exp.Select) and literal is not None and table is not None
    # The path is ours (a snapshot directory), and still goes in as a literal.
    literal.replace(exp.Literal.string(f"{source.table.path}/part-*.parquet"))
    table.set("alias", exp.TableAlias(this=exp.to_identifier(source.model.dataset, quoted=True)))
    return scan


def own_rules_scan(source: Source, user: UserContext) -> tuple[exp.Select, bool]:
    """`SELECT * FROM <snapshot> WHERE <the table's own rules>` (R1 alone). The
    second value is True when a rule exists but does not apply to this person's role.
    Use `Security.scan`, which adds R2: this is its first step."""
    alias = source.model.dataset
    scan = snapshot_scan(source)
    conditions = []
    bypassed = False
    for index, rule in enumerate(source.model.rls):
        if user.role not in rule.roles and user.may_be_exempt:
            bypassed = True
            continue
        try:
            conditions.append(
                expressions.rls_expression(rule.filter, alias, list(source.columns), user.values())
            )
        except ExpressionError as exc:
            # A broken rule must never fail open.
            raise ApiError(
                409,
                "invalid_model",
                f"A row-level security rule of {alias!r} is invalid, so its data cannot be "
                f"queried until the model is fixed (rule {index + 1}: {exc})",
            ) from exc
    if conditions:
        scan = scan.where(exp.and_(*[exp.Paren(this=c) for c in conditions]))
    return scan, bypassed


def key_value(
    source: Source,
    column: str,
    alias: str,
    other: Source,
    other_column: str,
    timezone: str | None,
) -> exp.Expr:
    """One column of a relationship's key, as queries and row-level security both
    compare it: a timestamp related to a date (a date table) is compared on its day,
    in the workspace timezone when the timestamp holds an instant."""
    value: exp.Expr = exp.column(column, table=alias, quoted=True)
    if source.columns.get(column) == "timestamp" and other.columns.get(other_column) == "date":
        if timezone is not None and column in source.timezones:
            value = exp.Anonymous(
                this="timezone", expressions=[exp.Literal.string(timezone), value]
            )
        value = exp.Cast(this=value, to=exp.DataType.build("DATE"))
    return value


@dataclass(frozen=True)
class _Edge:
    """`table` keeps only rows related to visible rows of `by`."""

    rel: Relationship
    table: str
    columns: tuple[str, ...]
    by: str
    by_columns: tuple[str, ...]


class Security:
    """R1 and R2 for one workspace: `sources` holds every table with data,
    `relationships` the workspace's relationships, and `timezone` the workspace
    timezone (a timestamp related to a date table is compared on its day there)."""

    def __init__(
        self,
        sources: Mapping[str, Source],
        relationships: Sequence[Relationship],
        timezone: str | None = None,
    ):
        self.sources = sources
        self.timezone = timezone
        self.relationships = tuple(relationships)
        self.edges: dict[str, list[_Edge]] = {}
        for rel in self.relationships:
            if not rel.active or rel.from_table == rel.to_table:
                continue
            self.edges.setdefault(rel.from_table, []).append(
                _Edge(rel, rel.from_table, rel.from_columns, rel.to_table, rel.to_columns)
            )
            if rel.security_filter == "both":
                self.edges.setdefault(rel.to_table, []).append(
                    _Edge(rel, rel.to_table, rel.to_columns, rel.from_table, rel.from_columns)
                )
        models = {name: source.model for name, source in sources.items()}
        self.loops = refused_loops(self.relationships, models)
        graph = active_edges(self.relationships)
        self.blocked: dict[str, tuple[str, ...]] = {}
        for loop in self.loops:
            for table in sorted(sources):
                if table in loop or set(loop) & reachable(table, graph):
                    self.blocked.setdefault(table, loop)

    def _visible(
        self, source: Source, user: UserContext, path: tuple[str, ...]
    ) -> tuple[exp.Select, bool, bool]:
        """The table's rows this person may see, whether a rule was passed over, and
        whether anything restricts them."""
        name = source.model.dataset
        scan, bypassed = own_rules_scan(source, user)
        restricted = scan.args.get("where") is not None
        conditions: list[exp.Expr] = []
        for edge in self.edges.get(name, []):
            target = self.sources.get(edge.by)
            if target is None or edge.by in path or edge.by == name:
                # No data to filter by, or the way back along the path.
                continue
            inner, inner_bypassed, inner_restricted = self._visible(target, user, (*path, name))
            bypassed = bypassed or inner_bypassed
            if not inner_restricted:
                continue
            for column in edge.columns:
                if column not in source.columns:
                    raise _missing_column(edge.rel, column, name)
            for column in edge.by_columns:
                if column not in target.columns:
                    raise _missing_column(edge.rel, column, edge.by)
            alias = f"_opsis_r2_{len(path)}"
            match = exp.and_(
                *[
                    exp.EQ(
                        this=key_value(target, remote, alias, source, local, self.timezone),
                        expression=key_value(source, local, name, target, remote, self.timezone),
                    )
                    for local, remote in zip(edge.columns, edge.by_columns, strict=True)
                ]
            )
            found = (
                exp.select(exp.Literal.number(1))
                .from_(inner.subquery(exp.to_identifier(alias, quoted=True)))
                .where(match)
            )
            conditions.append(exp.Exists(this=found))
        if conditions:
            scan = scan.where(exp.and_(*conditions))
            restricted = True
        return scan, bypassed, restricted

    def scan(self, source: Source, user: UserContext) -> tuple[exp.Select, bool]:
        """`SELECT * FROM <snapshot> WHERE <R1 and R2>`, and whether a rule was passed
        over for this person (the badge)."""
        loop = self.blocked.get(source.model.dataset)
        if loop is not None:
            raise LoopError(loop)
        select, bypassed, _ = self._visible(source, user, ())
        return select, bypassed

    def restricts(self, source: Source, user: UserContext) -> bool:
        """True when this person sees fewer than all of the table's rows (or might):
        a rule of its own applies to them, or R2 filters it."""
        if source.model.dataset in self.blocked:
            return True
        return self._visible(source, user, ())[2]


def _missing_column(rel: Relationship, column: str, table: str) -> ApiError:
    return ApiError(
        409,
        "relationship_column",
        f"The relationship {rel.describe()} uses {column!r}, which {table!r} does not have.",
    )
