"""Compile a semantic query into DuckDB SQL, as a sqlglot syntax tree.

Rules (brief section 6, ADR-031 and ADR-032):
- User input is never concatenated into SQL. Field references are looked up in
  the model, values become literals in the tree, and sqlglot renders the text.
- Every table is read through its secured rows (`Security.scan`): its own rules
  (R1) and the rules that reach it through relationships (R2), inside the
  subquery that reads it, so no join or filter can widen what a person sees.
- **Grouping:** a query may group by fields of any table its base table reaches
  along active relationships from the many side to the one side, any number of
  steps. The shortest path wins, two shortest paths are refused with both named,
  a path never goes through a restricted table, and after a left join the rest
  of the path stays left. Grouping by a table on the many side is refused, as it
  would count the base's rows more than once.
- **Filtering:** a filter on a table the base reaches that way is a condition on
  the joined table, as in v1. Otherwise it applies along relationships in their
  filter direction, as a semi-join (`EXISTS`), which never multiplies rows. The
  conditions on one table are applied together, as Power BI filters a table and
  then the tables it filters.
- **Measures** may come from several tables. The measures of each table (and of
  each relationship a measure names, as USERELATIONSHIP) are computed in a block
  of their own, every block grouped by the same fields, and the blocks are joined
  on them.
- Every query has a row cap and a timeout.

One block, which is v1's shape:

    SELECT <dimensions>, <time grain>, <measures>
    FROM (SELECT * FROM read_parquet('<snapshot>') WHERE <security>) AS "gifts"
    LEFT JOIN (SELECT * FROM read_parquet('<snapshot>') WHERE <security>) AS "donors" ON ...
    WHERE <dimension filters, segments, time range>
    GROUP BY <dimensions> HAVING <measure filters> ORDER BY ... LIMIT n

Several blocks:

    WITH "_b1" AS (SELECT <fields> AS "_g0", <aggregates> AS "_m0" FROM ... GROUP BY ...),
         "_b2" AS (...)
    SELECT COALESCE("_b1"."_g0", "_b2"."_g0") AS "<field>", <measures of the aggregates>
    FROM "_b1" FULL OUTER JOIN "_b2" ON "_b1"."_g0" IS NOT DISTINCT FROM "_b2"."_g0"
    WHERE <measure filters> ORDER BY ... LIMIT n
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlglot import exp

from opsis.connectors.base import ColumnType
from opsis.errors import ApiError
from opsis.query.engine import CompiledQuery, ResultColumn
from opsis.semantic import expressions
from opsis.semantic.calendar import CalendarConfig, resolve_relative_range
from opsis.semantic.expressions import ExpressionError
from opsis.semantic.model import Dimension, Measure, Segment
from opsis.semantic.relationships import (
    Path,
    Reach,
    Relationship,
    Step,
    describe_path,
    filter_steps,
    grouping_steps,
    shortest_paths,
    with_override,
)
from opsis.semantic.security import (
    Scan,
    Source,
    UserContext,
    key_value,
    own_rules_scan,
    snapshot_scan,
)
from opsis.semantic.workspace import Workspace

__all__ = [
    "Scan",
    "Source",
    "UserContext",
    "Workspace",
    "compile_query",
    "grain_expression",
    "own_rules_scan",
    "repeated_keys",
]

DIALECT = "duckdb"
DEFAULT_LIMIT = 500
MAX_TEXT_VALUE = expressions.MAX_TEXT_VALUE
MAX_INTEGER = 2**63 - 1
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
COMPARISONS = {
    "eq": exp.EQ,
    "neq": exp.NEQ,
    "gt": exp.GT,
    "gte": exp.GTE,
    "lt": exp.LT,
    "lte": exp.LTE,
}
TEXT_MATCH = {"contains": "contains", "starts_with": "starts_with", "ends_with": "ends_with"}
MEASURE_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "not_in"}
AGGREGATES: dict[str, type[exp.Func]] = {
    "sum": exp.Sum,
    "avg": exp.Avg,
    "min": exp.Min,
    "max": exp.Max,
}
FILTER_DIRECTION = (
    "along active relationships in their filter direction (from the one side to the many "
    "side, and back where crossFilter is both)"
)


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        if not any(i["path"] == path and i["message"] == message for i in self.items):
            self.items.append({"path": path, "message": message})

    def raise_if_any(self) -> None:
        if self.items:
            raise ApiError(422, "invalid_query", "The query is not valid.", self.items)


def _fail(path: str, message: str) -> ApiError:
    return ApiError(
        422, "invalid_query", "The query is not valid.", [{"path": path, "message": message}]
    )


def _parse_date(value: Any) -> date | None:
    """A date written exactly as YYYY-MM-DD, or None. Nothing looser is accepted."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _col(alias: str, name: str) -> exp.Column:
    return exp.column(name, table=alias, quoted=True)


def _date(value: date) -> exp.Expr:
    return exp.Cast(this=exp.Literal.string(value.isoformat()), to=exp.DataType.build("DATE"))


def _months(count: int) -> exp.Interval:
    return exp.Interval(this=exp.Literal.string(str(count)), unit=exp.var("MONTH"))


def _trunc(unit: str, value: exp.Expr) -> exp.Expr:
    return exp.Anonymous(this="date_trunc", expressions=[exp.Literal.string(unit), value])


def grain_expression(value: exp.Expr, grain: str, calendar: CalendarConfig) -> exp.Expr:
    """Truncate a date or timestamp to the start of its period, as a DATE.

    Must agree with opsis.semantic.calendar.period_start; tests compare the two.
    """
    shift = calendar.fiscal_year_start_month - 1
    if grain == "day":
        result = value
    elif grain == "week":
        if calendar.week_start == "monday":
            result = _trunc("week", value)  # DuckDB weeks start on Monday
        else:
            day = exp.Interval(this=exp.Literal.string("1"), unit=exp.var("DAY"))
            result = exp.Sub(
                this=_trunc("week", exp.Add(this=exp.Paren(this=value), expression=day)),
                expression=day.copy(),
            )
    elif grain in ("month", "fiscal_month"):
        result = _trunc("month", value)
    elif grain in ("quarter", "year"):
        result = _trunc(grain, value)
    elif grain in ("fiscal_quarter", "fiscal_year"):
        unit = "quarter" if grain == "fiscal_quarter" else "year"
        if shift == 0:
            result = _trunc(unit, value)
        else:
            shifted = exp.Sub(this=exp.Paren(this=value), expression=_months(shift))
            result = exp.Add(this=_trunc(unit, shifted), expression=_months(shift))
    else:  # the schema lists the grains, so this is a programming error
        raise ValueError(f"Unknown grain: {grain}")
    return exp.Cast(this=result, to=exp.DataType.build("DATE"))


def _turned(steps: Mapping[str, Sequence[Step]]) -> dict[str, list[Step]]:
    """Steps turned round: from the table filtered to the table that filters it."""
    result: dict[str, list[Step]] = {}
    for leaving in steps.values():
        for step in leaving:
            result.setdefault(step.end, []).append(Step(step.rel, not step.forward))
    return result


def _towards_base(path: Path) -> str:
    """A path found from the base, described from its far end to the base."""
    tables = [path[0].start, *(step.end for step in path)]
    return " → ".join(reversed(tables))


# ---- what a query is made of ---------------------------------------------------------


@dataclass
class _Block:
    """The measures of one table (with one relationship named, or none), read with
    the tables joined to group and filter them."""

    base: str
    override: str | None
    relationships: tuple[Relationship, ...]
    reach: Reach  # the tables the base reaches many-to-one
    joins: dict[str, tuple[Step, str]] = field(default_factory=dict)  # table: (step, join type)
    where: list[exp.Expr] = field(default_factory=list)
    aggregates: dict[tuple[str, str], str] = field(default_factory=dict)  # (table, measure): column
    filtered_by: Reach | None = None  # the tables that filter the base, found from it

    def reaches(self, table: str) -> bool:
        return table == self.base or table in self.joins or table in self.reach.paths


@dataclass(frozen=True)
class _Group:
    """A grouped field: a dimension, or the time dimension at a grain."""

    ref: str
    path: str
    source: Source
    dimension: Dimension
    grain: str | None = None

    @property
    def table(self) -> str:
        return self.source.model.dataset


@dataclass(frozen=True)
class _Condition:
    """A condition on dimensions (a filter, an `or` group, a segment or the time range),
    built for each block once it is known where each of its tables is read there."""

    path: str  # where a problem about reaching it is reported
    refs: tuple[str, ...]  # its fields, for messages
    tables: tuple[str, ...]
    build: Callable[[Mapping[str, str]], list[exp.Expr] | None]  # from each table's alias


@dataclass
class _Node:
    """A table read in a semi-join to filter a block, and the tables that filter it."""

    table: str
    alias: str
    step: Step  # from the table nearer the base to this one
    conditions: list[exp.Expr] = field(default_factory=list)
    children: list[str] = field(default_factory=list)


class _Compiler:
    def __init__(
        self,
        query: Mapping[str, Any],
        workspace: Workspace,
        user: UserContext,
        calendar: CalendarConfig,
        today: date,
        scan: Scan | None = None,
    ) -> None:
        self.query = query
        self.sources = workspace.sources
        self.relationships = workspace.relationships
        self.by_id = {rel.id: rel for rel in workspace.relationships}
        self.restricted = frozenset(name for name, s in self.sources.items() if s.restricted)
        self.user = user
        self.scan: Scan = scan or workspace.security.scan
        self.calendar = calendar
        self.today = today
        self.problems = _Problems()
        self.used: list[str] = []
        self.rls_bypassed = False
        self._aliases = 0
        named = query.get("dataset")
        if named is not None and str(named) not in self.sources:
            raise _fail("/dataset", f"There is no dataset called {named!r} with a model and data.")
        self.named = None if named is None else str(named)

    # ---- field lookup --------------------------------------------------------------

    def _field(self, path: str, ref: str) -> tuple[Source, str] | None:
        table, _, name = ref.partition(".")
        source = self.sources.get(table)
        if source is None:
            self.problems.add(
                path, f"{ref!r} is in {table!r}, which is not a table of the model with data."
            )
            return None
        return source, name

    def dimension(self, path: str, ref: str) -> tuple[Source, Dimension] | None:
        found = self._field(path, ref)
        if found is None:
            return None
        source, name = found
        dim = source.model.dimensions.get(name)
        if dim is None:
            kind = "a measure" if name in source.model.measures else "not defined"
            self.problems.add(path, f"{ref!r} is {kind}, so it cannot be used as a dimension here.")
            return None
        return source, dim

    def measure(self, path: str, ref: str) -> tuple[Source, Measure] | None:
        found = self._field(path, ref)
        if found is None:
            return None
        source, name = found
        measure = source.model.measures.get(name)
        if measure is None:
            kind = "a dimension" if name in source.model.dimensions else "not defined"
            self.problems.add(path, f"{ref!r} is {kind}, so it cannot be used as a measure here.")
            return None
        return source, measure

    def _is_measure(self, ref: str) -> bool:
        table, _, name = ref.partition(".")
        source = self.sources.get(table)
        return source is not None and name in source.model.measures

    # ---- blocks and joins ----------------------------------------------------------

    def _block(self, base: str, override: str | None) -> _Block:
        named = self.by_id.get(override) if override else None
        relationships = with_override(self.relationships, named)
        reach = shortest_paths(base, grouping_steps(relationships), blocked=self.restricted)
        return _Block(base, override, relationships, reach)

    def _join(self, block: _Block, table: str) -> None:
        """Join `table`, which the block reaches, and the tables on the way to it."""
        if table == block.base or table in block.joins:
            return
        kept_left = False
        for step in block.reach.paths[table]:
            kind = "left" if kept_left else step.rel.join_type  # after a left join, left
            kept_left = kept_left or kind == "left"
            if step.end not in block.joins:
                block.joins[step.end] = (step, kind)
                self._use(step.end)

    def _use(self, table: str) -> None:
        if table not in self.used:
            self.used.append(table)

    def _two_ways(self, ref: str, table: str, base: str, one: Path, other: Path) -> str:
        return (
            f"{ref!r} is in {table!r}, which {base!r} reaches by two paths as short as each "
            f"other ({describe_path(one)}, and {describe_path(other)}). Make one of the "
            "relationships inactive, or name one in a measure's `relationship`."
        )

    def _not_grouped(self, block: _Block, group: _Group, several: bool) -> str:
        """Why a block cannot group by a field."""
        table, base, ref = group.table, block.base, group.ref
        if table in block.reach.ambiguous:
            return self._two_ways(ref, table, base, *block.reach.ambiguous[table])
        steps = grouping_steps(block.relationships)
        free = shortest_paths(base, steps)
        if table in free.paths or table in free.ambiguous:
            return (
                f"{ref!r} is in {table!r}, which {base!r} reaches only through a restricted "
                "dataset, and a path never goes through one."
            )
        back = shortest_paths(table, steps)
        if base in back.paths or base in back.ambiguous:
            return (
                f"{ref!r} is on the many side of {base!r}: grouping by it would count rows "
                f"of {base!r} more than once. Group by fields of the tables {base!r} "
                f"reaches many-to-one, or use measures of {table!r}."
            )
        if several:
            return (
                f"{ref!r} cannot be used with the measures of {base!r}, which does not reach "
                f"{table!r} through active many-to-one relationships: every grouped field "
                "must reach the table of every measure."
            )
        return (
            f"{ref!r} is in {table!r}, which {base!r} does not reach through active "
            f"many-to-one relationships. Add a relationship from {base} to {table} in the "
            "Model view."
        )

    # ---- expressions ---------------------------------------------------------------

    def dimension_sql(self, source: Source, dim: Dimension, alias: str | None = None) -> exp.Expr:
        alias = alias or source.model.dataset
        if dim.column is not None:
            value: exp.Expr = _col(alias, dim.column)
        else:
            assert dim.expr is not None
            value = exp.Paren(
                this=expressions.column_expression(dim.expr, alias, list(source.columns))
            )
        return self._to_workspace_time(source, dim, value) if dim.type == "time" else value

    def _to_workspace_time(self, source: Source, dim: Dimension, value: exp.Expr) -> exp.Expr:
        """Express a timestamp as wall-clock time in the workspace timezone.

        Grains, ranges and filters all work on the result, so "by day" means
        the workspace's days. Three cases:
        - a column that holds instants (stored in UTC): convert;
        - a naive timestamp whose model says which zone it was written in
          (`timezone: UTC` for Salesforce): interpret it there, then convert;
        - any other naive timestamp, and every date: already workspace-local.
        """
        target = self.calendar.timezone
        if target is None:
            return value
        if dim.column is not None and source.columns.get(dim.column) == "date":
            return value

        def to_local(v: exp.Expr) -> exp.Expr:
            return exp.Anonymous(this="timezone", expressions=[exp.Literal.string(target), v])

        if dim.column is not None and dim.column in source.timezones:
            return to_local(value)  # TIMESTAMPTZ -> local TIMESTAMP
        if dim.timezone and dim.timezone != target:
            as_instant = exp.Anonymous(
                this="timezone", expressions=[exp.Literal.string(dim.timezone), value]
            )
            return to_local(as_instant)
        return value

    def _key(
        self, source: Source, column: str, alias: str, other: Source, other_column: str
    ) -> exp.Expr:
        """One column of a relationship's key (`security.key_value`)."""
        return key_value(source, column, alias, other, other_column, self.calendar.timezone)

    def _match(self, step: Step, start_alias: str, end_alias: str) -> exp.Expr:
        """The condition a step's two tables meet on."""
        start, end = self.sources[step.start], self.sources[step.end]
        return exp.and_(
            *[
                exp.EQ(
                    this=self._key(start, a, start_alias, end, b),
                    expression=self._key(end, b, end_alias, start, a),
                )
                for a, b in zip(step.start_columns, step.end_columns, strict=True)
            ]
        )

    def measure_sql(
        self, block: _Block, source: Source, measure: Measure, stack: tuple[str, ...] = ()
    ) -> exp.Expr:
        """A measure in a block of its own table: an aggregate, or a formula (which may
        use other tables' measures when they are in the same block)."""
        alias = source.model.dataset
        key = f"{alias}.{measure.name}"
        if key in stack:
            raise ExpressionError(f"Measure {measure.name!r} refers to itself.")
        if measure.expr is not None:

            def resolve(name: str) -> exp.Expr:
                owner, dot, other_name = name.partition(".")
                table, wanted = (owner, other_name) if dot else (alias, name)
                owner_source = self.sources.get(table)
                other = owner_source.model.measures.get(wanted) if owner_source else None
                if owner_source is None or other is None:
                    raise ExpressionError(f"There is no measure called {name!r}.")
                return self.measure_sql(block, owner_source, other, (*stack, key))

            return expressions.measure_expression(measure.expr, resolve)
        aggregate: exp.Expr
        if measure.agg == "count":
            aggregate = exp.Count(
                this=_col(alias, measure.column) if measure.column else exp.Star()
            )
        else:
            assert measure.column is not None
            column = _col(alias, measure.column)
            if measure.agg == "count_distinct":
                aggregate = exp.Count(this=exp.Distinct(expressions=[column]))
            else:
                aggregate = AGGREGATES[str(measure.agg)](this=column)
        if measure.filter is None:
            return aggregate
        # A conditional measure: agg(...) FILTER (WHERE condition). The condition may
        # name columns of tables the block reaches many-to-one, as table.column.
        related: dict[str, tuple[str, Sequence[str]]] = {}
        for table in sorted(expressions.qualified_tables(measure.filter)):
            if table in self.sources and table != alias and block.reaches(table):
                self._join(block, table)
                related[table] = (table, list(self.sources[table].columns))
        condition = expressions.filter_expression(
            measure.filter,
            alias,
            list(source.columns),
            {name: segment.filter for name, segment in source.model.segments.items()},
            related,
        )
        return exp.Filter(this=aggregate, expression=exp.Where(this=condition))

    def _value(self, path: str, value: Any, kind: str) -> exp.Expr | None:
        """A literal of the right type for a dimension or measure, or a problem.

        Every value from a query passes through here before it is rendered:
        numbers must be finite numbers, dates must parse as dates, booleans
        must be booleans, and text is length-capped and free of null bytes.
        sqlglot then escapes the literal. Nothing is ever spliced in as text.
        """
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, int | float):
                self.problems.add(path, f"Expected a number, got {value!r}.")
                return None
            if isinstance(value, float) and not math.isfinite(value):
                self.problems.add(path, "Expected a finite number.")
                return None
            if isinstance(value, int) and abs(value) > MAX_INTEGER:
                self.problems.add(path, "The number is too large.")
                return None
            return exp.Literal.number(value)
        if kind == "boolean":
            if not isinstance(value, bool):
                self.problems.add(path, f"Expected true or false, got {value!r}.")
                return None
            return exp.Boolean(this=value)
        if kind == "time":
            parsed = _parse_date(value)
            if parsed is None:
                self.problems.add(path, f"Expected a date such as 2026-01-31, got {value!r}.")
                return None
            return _date(parsed)
        if isinstance(value, bool):
            return exp.Literal.string("true" if value else "false")
        if isinstance(value, float) and not math.isfinite(value):
            self.problems.add(path, "Expected text or a finite number.")
            return None
        text = str(value)
        if len(text) > MAX_TEXT_VALUE:
            self.problems.add(path, f"The value is longer than {MAX_TEXT_VALUE} characters.")
            return None
        if "\x00" in text:
            self.problems.add(path, "The value contains a null character.")
            return None
        return exp.Literal.string(text)

    def _range(self, path: str, target: exp.Expr, spec: Mapping[str, Any]) -> list[exp.Expr]:
        if "relative" in spec:
            start, end = resolve_relative_range(str(spec["relative"]), self.today, self.calendar)
        else:
            start = _parse_date(spec["from"]) if "from" in spec else None
            end = _parse_date(spec["to"]) if "to" in spec else None
            if ("from" in spec and start is None) or ("to" in spec and end is None):
                self.problems.add(path, "Dates must look like 2026-01-31.")
                return []
            if start and end and start > end:
                self.problems.add(path, "`from` is after `to`.")
                return []
        conditions: list[exp.Expr] = []
        if start is not None:
            conditions.append(exp.GTE(this=target.copy(), expression=_date(start)))
        if end is not None:  # inclusive end, and correct for timestamps too
            conditions.append(exp.LT(this=target.copy(), expression=_date(end + timedelta(days=1))))
        return conditions

    def _test(
        self, path: str, spec: Mapping[str, Any], target: exp.Expr, kind: str
    ) -> exp.Expr | None:
        """The condition a filter puts on `target`, a dimension or measure of `kind`."""
        op = str(spec["op"])
        if op in ("is_null", "is_not_null"):
            test: exp.Expr = exp.Is(this=target, expression=exp.null())
            return exp.Not(this=test) if op == "is_not_null" else test
        if op == "in_range":
            if kind != "time":
                self.problems.add(f"{path}/op", "in_range needs a time dimension.")
                return None
            conditions = self._range(f"{path}/range", target, spec["range"])
            return exp.and_(*conditions) if conditions else exp.true()
        if op in TEXT_MATCH and kind != "string":
            self.problems.add(f"{path}/op", f"{op} needs a text dimension.")
            return None
        raw = spec.get("values") or []
        values = [self._value(f"{path}/values/{i}", v, kind) for i, v in enumerate(raw)]
        if any(v is None for v in values):
            return None
        literals = [v for v in values if v is not None]
        if op in COMPARISONS:
            return COMPARISONS[op](this=target, expression=literals[0])
        if op in ("in", "not_in"):
            test = exp.In(this=target, expressions=literals)
            return exp.Not(this=test) if op == "not_in" else test
        if op == "between":
            return exp.Between(this=target, low=literals[0], high=literals[1])
        # Case-insensitive text match, with the value as a literal (no LIKE wildcards).
        haystack = exp.Lower(this=exp.Cast(this=target, to=exp.DataType.build("VARCHAR")))
        needle = exp.Lower(this=literals[0])
        return exp.Anonymous(this=TEXT_MATCH[op], expressions=[haystack, needle])

    def _dimension_or_problem(
        self, path: str, source: Source, dim: Dimension, alias: str | None = None
    ) -> exp.Expr | None:
        try:
            return self.dimension_sql(source, dim, alias)
        except ExpressionError as exc:
            self.problems.add(path, f"Dimension {dim.name!r} has an invalid expression: {exc}")
            return None

    def _measure_or_problem(
        self, path: str, block: _Block, source: Source, measure: Measure
    ) -> exp.Expr | None:
        try:
            return self.measure_sql(block, source, measure)
        except ExpressionError as exc:
            self.problems.add(path, f"Measure {measure.name!r} has an invalid expression: {exc}")
            return None

    def _aggregates(
        self, path: str, source: Source, measure: Measure, stack: tuple[str, ...] = ()
    ) -> list[tuple[str, Measure]]:
        """The aggregates a measure is made of (itself, or those its formula uses, in
        any table), each with its table."""
        table = source.model.dataset
        key = f"{table}.{measure.name}"
        if key in stack:
            self.problems.add(path, f"Measure {measure.name!r} refers to itself.")
            return []
        if measure.expr is None:
            if measure.relationship and measure.relationship not in self.by_id:
                self.problems.add(
                    path,
                    f"Measure {measure.name!r} names the relationship "
                    f"{measure.relationship!r}, which does not exist.",
                )
                return []
            return [(table, measure)]
        found: list[tuple[str, Measure]] = []
        for reference in expressions.measure_references(measure.expr):
            owner, dot, name = reference.partition(".")
            other_table, other_name = (owner, name) if dot else (table, reference)
            other_source = self.sources.get(other_table)
            other = other_source.model.measures.get(other_name) if other_source else None
            if other_source is None or other is None:
                self.problems.add(
                    path, f"Measure {measure.name!r} uses {reference!r}, which does not exist."
                )
                continue
            found += self._aggregates(path, other_source, other, (*stack, key))
        return found

    # ---- reading tables ------------------------------------------------------------

    def _read(self, source: Source, alias: str | None = None) -> exp.Expr:
        """A table's rows as this person may see them, as a FROM item."""
        scan, bypassed = self.scan(source, self.user)
        if bypassed:
            self.rls_bypassed = True  # reported to the user, never silent
        return scan.subquery(exp.to_identifier(alias or source.model.dataset, quoted=True))

    def _alias(self) -> str:
        self._aliases += 1
        return f"_opsis_f{self._aliases}"

    # ---- conditions ----------------------------------------------------------------

    def _filter_condition(self, path: str, spec: Mapping[str, Any]) -> _Condition | None:
        field_path = f"{path}/field"
        ref = str(spec["field"])
        found = self.dimension(field_path, ref)
        if found is None:
            return None
        source, dim = found
        table = source.model.dataset

        def build(aliases: Mapping[str, str]) -> list[exp.Expr] | None:
            target = self._dimension_or_problem(field_path, source, dim, aliases[table])
            test = None if target is None else self._test(path, spec, target, dim.type)
            return None if test is None else [test]

        return _Condition(field_path, (ref,), (table,), build)

    def _or_condition(self, path: str, spec: Mapping[str, Any]) -> _Condition | None:
        alternatives: list[list[_Condition]] = []
        for i, alternative in enumerate(spec["or"]):
            options: list[_Condition] = []
            for j, inner in enumerate(alternative):
                inner_path = f"{path}/or/{i}/{j}"
                if self._is_measure(str(inner["field"])):
                    self.problems.add(
                        f"{inner_path}/field", "Inside an `or` group, filter on dimensions only."
                    )
                    continue
                made = self._filter_condition(inner_path, inner)
                if made is not None:
                    options.append(made)
            if len(options) < len(alternative):
                return None  # the problems are reported
            alternatives.append(options)
        everything = [option for options in alternatives for option in options]
        tables = tuple(dict.fromkeys(t for option in everything for t in option.tables))
        refs = tuple(dict.fromkeys(r for option in everything for r in option.refs))

        def build(aliases: Mapping[str, str]) -> list[exp.Expr] | None:
            parts: list[exp.Expr] = []
            for options in alternatives:
                built = [option.build(aliases) for option in options]
                if any(b is None for b in built):
                    return None
                tests = [test for b in built if b is not None for test in b]
                parts.append(exp.and_(*tests) if len(tests) > 1 else tests[0])
            return [exp.Paren(this=exp.or_(*parts)) if len(parts) > 1 else parts[0]]

        return _Condition(path, refs, tables, build)

    def _time_condition(
        self, spec: Mapping[str, Any], source: Source, dim: Dimension
    ) -> _Condition:
        table = source.model.dataset

        def build(aliases: Mapping[str, str]) -> list[exp.Expr] | None:
            target = self._dimension_or_problem("/timeDimension/field", source, dim, aliases[table])
            if target is None:
                return None
            return self._range("/timeDimension/range", target, spec["range"])

        return _Condition("/timeDimension/field", (str(spec["field"]),), (table,), build)

    def _segment_condition(
        self, path: str, ref: str, source: Source, segment: Segment
    ) -> _Condition:
        table = source.model.dataset

        def build(aliases: Mapping[str, str]) -> list[exp.Expr] | None:
            try:
                condition = expressions.column_expression(
                    segment.filter, aliases[table], list(source.columns)
                )
            except ExpressionError as exc:
                self.problems.add(path, f"Segment {segment.name!r} has an invalid filter: {exc}")
                return None
            return [exp.Paren(this=condition)]

        return _Condition(path, (ref,), (table,), build)

    def _filtered_by(self, block: _Block) -> Reach:
        """The tables whose filters reach the block's base, each with its path from
        the base."""
        if block.filtered_by is None:
            turned = _turned(filter_steps(block.relationships))
            block.filtered_by = shortest_paths(block.base, turned, blocked=self.restricted)
        return block.filtered_by

    def _place(self, block: _Block, conditions: Sequence[_Condition]) -> list[bool]:
        """Add each condition to the block: on the joined tables when the base reaches
        all of its tables many-to-one, or else in a semi-join on the one table it is on.
        Returns, for each, False when it does not reach the block (a condition that
        cannot be placed for another reason has a problem reported)."""
        nodes: dict[str, _Node] = {}
        roots: list[str] = []
        reached: list[bool] = []
        for condition in conditions:
            reached.append(True)
            if all(block.reaches(table) for table in condition.tables):
                for table in condition.tables:
                    self._join(block, table)
                block.where.extend(condition.build({t: t for t in condition.tables}) or [])
                continue
            two_ways = next((t for t in condition.tables if t in block.reach.ambiguous), None)
            if two_ways is not None:
                self.problems.add(
                    condition.path,
                    self._two_ways(
                        condition.refs[0], two_ways, block.base, *block.reach.ambiguous[two_ways]
                    ),
                )
                continue
            if len(condition.tables) > 1:
                self.problems.add(
                    condition.path,
                    "The fields of an `or` group must all be in one table, or in tables "
                    f"{block.base!r} reaches through many-to-one relationships.",
                )
                continue
            table = condition.tables[0]
            found = self._filtered_by(block)
            if table in found.ambiguous:
                one, other = found.ambiguous[table]
                self.problems.add(
                    condition.path,
                    f"{condition.refs[0]!r} filters {block.base!r} along two paths as short as "
                    f"each other ({_towards_base(one)}, and {_towards_base(other)}). Make one "
                    "of the relationships inactive.",
                )
                continue
            path = found.paths.get(table)
            if path is None:
                reached[-1] = False
                continue
            for step in path:
                if step.end not in nodes:
                    nodes[step.end] = _Node(step.end, self._alias(), step)
                    parent = roots if step.start == block.base else nodes[step.start].children
                    parent.append(step.end)
            nodes[table].conditions.extend(condition.build({table: nodes[table].alias}) or [])
        for table in roots:
            block.where.append(self._exists(nodes, table, block.base))
        return reached

    def _exists(self, nodes: Mapping[str, _Node], table: str, outer: str) -> exp.Expr:
        """`EXISTS (SELECT 1 FROM <table's rows> WHERE <it matches the outer row> AND
        <its conditions> AND <the semi-joins of the tables that filter it>)`."""
        node = nodes[table]
        self._use(table)
        conditions = [self._match(node.step, outer, node.alias), *node.conditions]
        conditions += [self._exists(nodes, child, node.alias) for child in node.children]
        rows = (
            exp.select(exp.Literal.number(1))
            .from_(self._read(self.sources[table], node.alias))
            .where(exp.and_(*conditions))
        )
        return exp.Exists(this=rows)

    def _unreached(self, condition: _Condition, blocks: Sequence[_Block]) -> str:
        table, ref = condition.tables[0], condition.refs[0]
        restricted = ""
        for block in blocks:
            free_filters = shortest_paths(block.base, _turned(filter_steps(block.relationships)))
            free_groups = shortest_paths(block.base, grouping_steps(block.relationships))
            if table in free_filters.paths or table in free_groups.paths:
                restricted = " (a path never goes through a restricted dataset)"
        if len(blocks) == 1:
            base = blocks[0].base
            return (
                f"{ref!r} does not filter {base!r}: no path leads from {table!r} to {base!r} "
                f"{FILTER_DIRECTION}{restricted}."
            )
        names = ", ".join(dict.fromkeys(repr(b.base) for b in blocks))
        return (
            f"{ref!r} filters none of the measures' tables ({names}): no path leads from "
            f"{table!r} to any of them {FILTER_DIRECTION}{restricted}."
        )

    # ---- the whole query -----------------------------------------------------------

    def compile(
        self, max_rows: int, timeout_s: float
    ) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
        query = self.query

        groups: list[_Group] = []
        for index, ref in enumerate(query.get("dimensions") or []):
            path = f"/dimensions/{index}"
            found = self.dimension(path, ref)
            if found is not None:
                groups.append(_Group(ref, path, *found))
        conditions: list[_Condition] = []
        time_spec = query.get("timeDimension")
        if time_spec:
            ref = str(time_spec["field"])
            found = self.dimension("/timeDimension/field", ref)
            if found is not None:
                source, dim = found
                grain = time_spec.get("grain")
                if dim.type != "time":
                    self.problems.add("/timeDimension/field", f"{ref!r} is not a time dimension.")
                else:
                    if grain and grain not in dim.grains:
                        self.problems.add(
                            "/timeDimension/grain",
                            f"{ref!r} allows these grains: {', '.join(dim.grains)}.",
                        )
                    elif grain:
                        groups.append(_Group(ref, "/timeDimension/field", source, dim, grain))
                    if "range" in time_spec:
                        conditions.append(self._time_condition(time_spec, source, dim))

        selected: list[tuple[str, str, Source, Measure]] = []
        for index, ref in enumerate(query.get("measures") or []):
            path = f"/measures/{index}"
            found_measure = self.measure(path, ref)
            if found_measure is not None:
                selected.append((ref, path, *found_measure))
        measure_filters: list[tuple[int, Mapping[str, Any], Source, Measure]] = []
        for index, spec in enumerate(query.get("filters") or []):
            path = f"/filters/{index}"
            if "or" in spec:
                made = self._or_condition(path, spec)
            elif self._is_measure(str(spec["field"])):
                found_measure = self.measure(f"{path}/field", str(spec["field"]))
                if found_measure is None:
                    continue
                if str(spec["op"]) not in MEASURE_OPS:
                    self.problems.add(f"{path}/op", f"{spec['op']} cannot be used on a measure.")
                else:
                    measure_filters.append((index, spec, *found_measure))
                continue
            else:
                made = self._filter_condition(path, spec)
            if made is not None:
                conditions.append(made)
        for index, ref in enumerate(query.get("segments") or []):
            path = f"/segments/{index}"
            found_field = self._field(path, ref)
            if found_field is None:
                continue
            source, name = found_field
            segment = source.model.segments.get(name)
            if segment is None:
                self.problems.add(path, f"There is no segment called {ref!r}.")
                continue
            conditions.append(self._segment_condition(path, ref, source, segment))

        # One block for each table (and relationship named) whose aggregates are used.
        keys: list[tuple[str, str | None]] = []
        wanted = [(path, source, measure) for _, path, source, measure in selected] + [
            (f"/filters/{index}", source, measure) for index, _, source, measure in measure_filters
        ]
        for path, source, measure in wanted:
            for table, aggregate in self._aggregates(path, source, measure):
                if (table, aggregate.relationship) not in keys:
                    keys.append((table, aggregate.relationship))
        if not keys:
            asked = bool(query.get("measures")) or any(
                "field" in spec and self._is_measure(str(spec["field"]))
                for spec in query.get("filters") or []
            )
            base = self.named or (None if asked else self._infer_base(groups))
            if base is not None:
                keys.append((base, None))
        if not keys:
            if not self.problems.items:
                self.problems.add("/", "Select at least one dimension, measure or time grain.")
            self.problems.raise_if_any()

        blocks = [self._block(table, override) for table, override in keys]
        for block in blocks:
            self._use(block.base)
        for block in blocks:
            for group in groups:
                if block.reaches(group.table):
                    self._join(block, group.table)
                else:
                    self.problems.add(group.path, self._not_grouped(block, group, len(blocks) > 1))
        placed = [self._place(block, conditions) for block in blocks]
        for index, condition in enumerate(conditions):
            if not any(reached[index] for reached in placed):
                self.problems.add(condition.path, self._unreached(condition, blocks))

        if len(blocks) == 1:
            return self._single(blocks[0], groups, selected, measure_filters, max_rows, timeout_s)
        return self._several(blocks, groups, selected, measure_filters, max_rows, timeout_s)

    def _infer_base(self, groups: Sequence[_Group]) -> str | None:
        """With no measures and no `dataset`: the first grouped field's table that
        reaches all the others."""
        tables = list(dict.fromkeys(group.table for group in groups))
        if not tables:
            return None
        steps = grouping_steps(self.relationships)
        for candidate in tables:
            reach = shortest_paths(candidate, steps, blocked=self.restricted)
            if all(table == candidate or table in reach.paths for table in tables):
                return candidate
        self.problems.add(
            "/dimensions",
            f"None of {', '.join(repr(t) for t in tables)} reaches all the others through "
            "many-to-one relationships, so there is no table to list them from. Add a "
            "measure, or name the table to list them from in `dataset`.",
        )
        return None

    def _group_projections(
        self, block: _Block, groups: Sequence[_Group], positional: bool
    ) -> tuple[list[exp.Expr], list[ResultColumn]]:
        projections: list[exp.Expr] = []
        columns: list[ResultColumn] = []
        for index, group in enumerate(groups):
            if not block.reaches(group.table):
                continue  # reported
            sql = self._dimension_or_problem(group.path, group.source, group.dimension)
            if sql is None:
                continue
            if group.grain:
                sql = grain_expression(sql, group.grain, self.calendar)
                column = ResultColumn(
                    group.ref, ColumnType.DATE, "time", label=group.dimension.label,
                    grain=group.grain,
                )  # fmt: skip
            else:
                column = ResultColumn(
                    group.ref,
                    _dimension_type(group.source, group.dimension),
                    "dimension",
                    label=group.dimension.label,
                )
            alias = f"_g{index}" if positional else group.ref
            projections.append(exp.alias_(sql, alias, quoted=True))
            columns.append(column)
        return projections, columns

    def _from(self, block: _Block, projections: Sequence[exp.Expr]) -> exp.Select:
        statement = exp.select(*projections).from_(self._read(self.sources[block.base]))
        for table, (step, kind) in block.joins.items():
            statement = statement.join(
                self._read(self.sources[table]),
                on=self._match(step, step.start, step.end),
                join_type=kind,
            )
        if block.where:
            statement = statement.where(exp.and_(*block.where))
        return statement

    def _order(self, columns: Sequence[ResultColumn]) -> list[exp.Expr]:
        selected = {c.name for c in columns}
        order: list[exp.Expr] = []
        for index, spec in enumerate(self.query.get("sort") or []):
            if spec["field"] not in selected:
                self.problems.add(
                    f"/sort/{index}/field", "Sort by a field that is selected in the query."
                )
                continue
            order.append(
                exp.Ordered(
                    this=exp.column(spec["field"], quoted=True),
                    desc=spec.get("dir", "asc") == "desc",
                    nulls_first=False,
                )
            )
        if not order:
            time_column = next((c for c in columns if c.role == "time"), None)
            if time_column is not None:
                order.append(
                    exp.Ordered(
                        this=exp.column(time_column.name, quoted=True),
                        desc=False,
                        nulls_first=False,
                    )
                )
        return order

    def _repeated(self, columns: Sequence[ResultColumn]) -> None:
        names = [c.name for c in columns]
        for name in dict.fromkeys(names):
            if names.count(name) > 1:
                self.problems.add("/", f"{name!r} is selected more than once.")

    def _finish(
        self,
        statement: exp.Select,
        order: list[exp.Expr],
        columns: list[ResultColumn],
        timeout_s: float,
        max_rows: int,
    ) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
        limit = min(int(self.query.get("limit") or DEFAULT_LIMIT), max_rows)
        if order:
            statement = statement.order_by(*order)
        # One extra row tells the engine whether the result was cut off.
        statement = statement.limit(limit + 1)
        if self.query.get("offset"):
            statement = statement.offset(int(self.query["offset"]))
        text = statement.sql(dialect=DIALECT, comments=False)
        tables = tuple(self.sources[name].table for name in self.used)
        compiled = CompiledQuery(text, (), tables, limit, timeout_s, self.rls_bypassed)
        return compiled, tuple(columns)

    def _single(
        self,
        block: _Block,
        groups: Sequence[_Group],
        selected: Sequence[tuple[str, str, Source, Measure]],
        measure_filters: Sequence[tuple[int, Mapping[str, Any], Source, Measure]],
        max_rows: int,
        timeout_s: float,
    ) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
        projections, columns = self._group_projections(block, groups, positional=False)
        group_count = len(projections)
        for ref, path, source, measure in selected:
            sql = self._measure_or_problem(path, block, source, measure)
            if sql is not None:
                projections.append(exp.alias_(sql, ref, quoted=True))
                columns.append(_measure_column(ref, source, measure))
        self._repeated(columns)
        having: list[exp.Expr] = []
        for index, spec, source, measure in measure_filters:
            path = f"/filters/{index}"
            target = self._measure_or_problem(path, block, source, measure)
            test = None if target is None else self._test(path, spec, target, "number")
            if test is not None:
                having.append(test)
        order = self._order(columns)
        if not projections and not self.problems.items:
            self.problems.add("/", "Select at least one dimension, measure or time grain.")
        self.problems.raise_if_any()

        statement = self._from(block, projections)
        if group_count:
            statement = statement.group_by(*[exp.Literal.number(i + 1) for i in range(group_count)])
        if having:
            statement = statement.having(exp.and_(*having))
        return self._finish(statement, order, columns, timeout_s, max_rows)

    def _several(
        self,
        blocks: Sequence[_Block],
        groups: Sequence[_Group],
        selected: Sequence[tuple[str, str, Source, Measure]],
        measure_filters: Sequence[tuple[int, Mapping[str, Any], Source, Measure]],
        max_rows: int,
        timeout_s: float,
    ) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
        """Each block grouped by the same fields, as a CTE, and the CTEs joined on
        them (null-safe, so an empty field value matches)."""
        names = [f"_b{i + 1}" for i in range(len(blocks))]
        by_key = {(b.base, b.override): (b, n) for b, n in zip(blocks, names, strict=True)}
        wanted = [(path, source, measure) for _, path, source, measure in selected] + [
            (f"/filters/{index}", source, measure) for index, _, source, measure in measure_filters
        ]
        ctes: list[tuple[str, exp.Select]] = []
        columns: list[ResultColumn] = []
        fields: list[str] = []
        for block, name in zip(blocks, names, strict=True):
            aggregates: list[exp.Expr] = []
            for path, source, measure in wanted:
                for table, aggregate in self._aggregates(path, source, measure):
                    key = (table, aggregate.name)
                    if (table, aggregate.relationship) != (block.base, block.override):
                        continue
                    if key in block.aggregates:
                        continue
                    sql = self._measure_or_problem(path, block, self.sources[table], aggregate)
                    if sql is None:
                        continue
                    block.aggregates[key] = f"_m{len(block.aggregates)}"
                    aggregates.append(exp.alias_(sql, block.aggregates[key], quoted=True))
            projections, block_columns = self._group_projections(block, groups, positional=True)
            if not ctes:
                columns, fields = block_columns, [p.alias for p in projections]
            statement = self._from(block, [*projections, *aggregates])
            if projections:
                statement = statement.group_by(
                    *[exp.Literal.number(i + 1) for i in range(len(projections))]
                )
            ctes.append((name, statement))

        def value(
            path: str, source: Source, measure: Measure, stack: tuple[str, ...] = ()
        ) -> exp.Expr | None:
            """A measure from the blocks' aggregates."""
            table = source.model.dataset
            key = f"{table}.{measure.name}"
            if key in stack:
                return None  # reported by _aggregates
            if measure.expr is None:
                found = by_key.get((table, measure.relationship))
                if found is None or (table, measure.name) not in found[0].aggregates:
                    return None
                return _col(found[1], found[0].aggregates[(table, measure.name)])

            def resolve(ref: str) -> exp.Expr:
                owner, dot, other_name = ref.partition(".")
                other_table, wanted_name = (owner, other_name) if dot else (table, ref)
                other_source = self.sources.get(other_table)
                other = other_source.model.measures.get(wanted_name) if other_source else None
                result = None
                if other_source is not None and other is not None:
                    result = value(path, other_source, other, (*stack, key))
                if result is None:
                    raise ExpressionError(f"{ref!r} could not be computed.")
                return result

            try:
                return expressions.measure_expression(measure.expr, resolve)
            except ExpressionError as exc:
                self.problems.add(
                    path, f"Measure {measure.name!r} has an invalid expression: {exc}"
                )
                return None

        def first_of(column: str, among: Sequence[str]) -> exp.Expr:
            values = [_col(name, column) for name in among]
            return values[0] if len(values) == 1 else exp.Coalesce(
                this=values[0], expressions=values[1:]
            )  # fmt: skip

        projections = [
            exp.alias_(first_of(column, names), columns[position].name, quoted=True)
            for position, column in enumerate(fields)
        ]
        for ref, path, source, measure in selected:
            sql = value(path, source, measure)
            if sql is not None:
                projections.append(exp.alias_(sql, ref, quoted=True))
                columns.append(_measure_column(ref, source, measure))
        self._repeated(columns)
        where: list[exp.Expr] = []
        for index, spec, source, measure in measure_filters:
            path = f"/filters/{index}"
            target = value(path, source, measure)
            test = None if target is None else self._test(path, spec, target, "number")
            if test is not None:
                where.append(test)
        order = self._order(columns)
        self.problems.raise_if_any()

        statement = exp.select(*projections).from_(exp.table_(names[0], quoted=True))
        for position in range(1, len(names)):
            right = exp.table_(names[position], quoted=True)
            if fields:
                on = exp.and_(
                    *[
                        exp.NullSafeEQ(
                            this=first_of(column, names[:position]),
                            expression=_col(names[position], column),
                        )
                        for column in fields
                    ]
                )
                statement = statement.join(right, on=on, join_type="full outer")
            else:
                statement = statement.join(right, join_type="cross")
        for name, cte in ctes:
            statement = statement.with_(exp.to_identifier(name, quoted=True), as_=cte)
        if where:
            statement = statement.where(exp.and_(*where))
        return self._finish(statement, order, columns, timeout_s, max_rows)


def _measure_column(ref: str, source: Source, measure: Measure) -> ResultColumn:
    return ResultColumn(
        ref, _measure_type(source, measure), "measure", format=measure.format, label=measure.label
    )


def _dimension_type(source: Source, dim: Dimension) -> ColumnType:
    if dim.column is not None and dim.column in source.columns:
        return ColumnType(source.columns[dim.column])
    return {
        "number": ColumnType.FLOAT,
        "boolean": ColumnType.BOOLEAN,
        "time": ColumnType.TIMESTAMP,
    }.get(dim.type, ColumnType.STRING)


def _measure_type(source: Source, measure: Measure) -> ColumnType:
    if measure.agg in ("count", "count_distinct"):
        return ColumnType.INTEGER
    if measure.agg in ("sum", "min", "max") and measure.column in source.columns:
        return ColumnType(source.columns[str(measure.column)])
    return ColumnType.FLOAT


def repeated_keys(source: Source, columns: Sequence[str], examples: int) -> exp.Select:
    """The values of `columns` (a relationship's key on its one side) that are on more
    than one row, the most repeated first. Each result row holds the key's values, how
    many rows have them, and how many such values there are in all. A row with an
    empty key column matches nothing, so it is left out.

    It reads every row, whoever asks: the caller shows the values only to people
    who may see all of the dataset (see services.model_warnings).
    """
    keys = [exp.column(name, quoted=True) for name in columns]
    rows = exp.Count(this=exp.Star())
    return (
        exp.select(
            *[key.copy() for key in keys],
            exp.alias_(rows.copy(), "_opsis_rows", quoted=True),
            # A window runs after HAVING, so this counts the repeated values.
            exp.alias_(exp.Window(this=rows.copy()), "_opsis_values", quoted=True),
        )
        .from_(snapshot_scan(source).subquery(exp.to_identifier("keys", quoted=True)))
        .where(
            exp.and_(
                *[exp.Not(this=exp.Is(this=key.copy(), expression=exp.null())) for key in keys]
            )
        )
        .group_by(*[key.copy() for key in keys])
        .having(exp.GT(this=rows.copy(), expression=exp.Literal.number(1)))
        .order_by(
            exp.Ordered(this=exp.column("_opsis_rows", quoted=True), desc=True),
            *[exp.Ordered(this=key.copy()) for key in keys],
        )
        .limit(examples)
    )


def compile_query(
    query: Mapping[str, Any],
    workspace: Workspace | Mapping[str, Source],
    user: UserContext,
    calendar: CalendarConfig,
    today: date,
    *,
    max_rows: int,
    timeout_s: float,
    scan: Scan | None = None,
) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
    """Compile a schema-valid semantic query. Raises ApiError(422) with paths on problems.

    `workspace` holds the workspace model's tables with data and its relationships. A
    plain mapping of sources is read as v1 models, whose joins become relationships by
    the migration's rules. `scan` reads each table for the person: the default applies
    R1 and R2 (`Security.scan`)."""
    if not isinstance(workspace, Workspace):
        workspace = Workspace.from_v1(workspace)
    return _Compiler(query, workspace, user, calendar, today, scan).compile(max_rows, timeout_s)
