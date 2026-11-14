"""Compile a semantic query into one DuckDB SELECT, as a sqlglot syntax tree.

Rules (brief section 6):
- User input is never concatenated into SQL. Field references are looked up in
  the model, values become literals in the tree, and sqlglot renders the text.
- Row-level security is always applied, inside the subquery that reads each
  dataset, so no join or filter can widen what a user sees.
- Every query has a row cap and a timeout.

Shape of the result:

    SELECT <dimensions>, <time grain>, <measures>
    FROM (SELECT * FROM read_parquet('<snapshot>') WHERE <rls>) AS "gifts"
    LEFT JOIN (SELECT * FROM read_parquet('<snapshot>') WHERE <rls>) AS "donors" ON ...
    WHERE <dimension filters, segments, time range>
    GROUP BY <dimensions> HAVING <measure filters> ORDER BY ... LIMIT n
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import sqlglot
from sqlglot import exp

from opsis.connectors.base import ColumnType
from opsis.errors import ApiError
from opsis.query.engine import CompiledQuery, ResultColumn, TableRef
from opsis.semantic import expressions
from opsis.semantic.calendar import CalendarConfig, resolve_relative_range
from opsis.semantic.expressions import ExpressionError
from opsis.semantic.model import SAFE_JOINS, Dimension, Measure, Model

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


@dataclass(frozen=True)
class Source:
    """One dataset as the compiler sees it: its model, columns and current snapshot."""

    model: Model
    columns: Mapping[str, str]  # column name -> logical type
    table: TableRef
    # Timestamp columns that hold instants, and the zone they are stored in (UTC).
    timezones: Mapping[str, str] = field(default_factory=dict)


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


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
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


class _Compiler:
    def __init__(
        self,
        query: Mapping[str, Any],
        sources: Mapping[str, Source],
        user: UserContext,
        calendar: CalendarConfig,
        today: date,
    ) -> None:
        self.query = query
        self.sources = sources
        self.user = user
        self.calendar = calendar
        self.today = today
        self.problems = _Problems()
        base_name = str(query["dataset"])
        if base_name not in sources:
            raise _fail(
                "/dataset", f"There is no dataset called {base_name!r} with a model and data."
            )
        self.base = sources[base_name]
        self.used: list[str] = [base_name]
        self.rls_bypassed = False

    # ---- field lookup --------------------------------------------------------------

    def _source(self, path: str, ref: str) -> tuple[Source, str] | None:
        dataset, _, name = ref.partition(".")
        base_name = self.base.model.dataset
        if dataset != base_name:
            join = next((j for j in self.base.model.joins if j.to == dataset), None)
            if join is None or dataset not in self.sources:
                self.problems.add(
                    path,
                    f"{ref!r} is in dataset {dataset!r}, which {base_name!r} has no join to. "
                    f"Add a join in the {base_name} model.",
                )
                return None
            if join.relationship not in SAFE_JOINS:
                self.problems.add(
                    path,
                    f"The join from {base_name!r} to {dataset!r} is {join.relationship}, which "
                    "would double-count measures. Query from the other dataset instead.",
                )
                return None
            if dataset not in self.used:
                self.used.append(dataset)
        return self.sources[dataset], name

    def dimension(self, path: str, ref: str) -> tuple[Source, Dimension] | None:
        found = self._source(path, ref)
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
        dataset, _, name = ref.partition(".")
        if dataset != self.base.model.dataset:
            self.problems.add(
                path,
                f"Measures must come from the query's own dataset ({self.base.model.dataset}).",
            )
            return None
        found = self.base.model.measures.get(name)
        if found is None:
            kind = "a dimension" if name in self.base.model.dimensions else "not defined"
            self.problems.add(path, f"{ref!r} is {kind}, so it cannot be used as a measure here.")
            return None
        return self.base, found

    # ---- expressions ---------------------------------------------------------------

    def dimension_sql(self, source: Source, dim: Dimension) -> exp.Expr:
        alias = source.model.dataset
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

    def measure_sql(
        self, source: Source, measure: Measure, stack: tuple[str, ...] = ()
    ) -> exp.Expr:
        alias = source.model.dataset
        if measure.name in stack:
            raise ExpressionError(f"Measure {measure.name!r} refers to itself.")
        if measure.expr is not None:

            def resolve(name: str) -> exp.Expr:
                other = source.model.measures.get(name)
                if other is None:
                    raise ExpressionError(f"There is no measure called {name!r}.")
                return self.measure_sql(source, other, (*stack, measure.name))

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
        # A conditional measure: agg(...) FILTER (WHERE condition).
        condition = expressions.filter_expression(
            measure.filter,
            alias,
            list(source.columns),
            {name: segment.filter for name, segment in source.model.segments.items()},
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

    def _filter(self, index: int, spec: Mapping[str, Any]) -> tuple[exp.Expr | None, bool]:
        """Returns (condition, is_measure_filter)."""
        path = f"/filters/{index}"
        ref, op = str(spec["field"]), str(spec["op"])
        name = ref.partition(".")[2]
        is_measure = (
            ref.partition(".")[0] == self.base.model.dataset and name in self.base.model.measures
        )
        if is_measure:
            found_measure = self.measure(f"{path}/field", ref)
            if found_measure is None:
                return None, True
            if op not in MEASURE_OPS:
                self.problems.add(f"{path}/op", f"{op} cannot be used on a measure.")
                return None, True
            target, kind = self._measure_or_problem(path, *found_measure), "number"
            if target is None:
                return None, True
        else:
            found = self.dimension(f"{path}/field", ref)
            if found is None:
                return None, False
            source, dim = found
            target, kind = self._dimension_or_problem(f"{path}/field", source, dim), dim.type
            if target is None:
                return None, False

        if op in ("is_null", "is_not_null"):
            test: exp.Expr = exp.Is(this=target, expression=exp.null())
            return (exp.Not(this=test) if op == "is_not_null" else test), is_measure
        if op == "in_range":
            if kind != "time":
                self.problems.add(f"{path}/op", "in_range needs a time dimension.")
                return None, is_measure
            conditions = self._range(f"{path}/range", target, spec["range"])
            return (exp.and_(*conditions) if conditions else exp.true()), is_measure

        if op in TEXT_MATCH and kind != "string":
            self.problems.add(f"{path}/op", f"{op} needs a text dimension.")
            return None, is_measure
        raw = spec.get("values") or []
        values = [self._value(f"{path}/values/{i}", v, kind) for i, v in enumerate(raw)]
        if any(v is None for v in values):
            return None, is_measure
        literals = [v for v in values if v is not None]
        if op in COMPARISONS:
            return COMPARISONS[op](this=target, expression=literals[0]), is_measure
        if op in ("in", "not_in"):
            test = exp.In(this=target, expressions=literals)
            return (exp.Not(this=test) if op == "not_in" else test), is_measure
        if op == "between":
            return exp.Between(this=target, low=literals[0], high=literals[1]), is_measure
        # Case-insensitive text match, with the value as a literal (no LIKE wildcards).
        haystack = exp.Lower(this=exp.Cast(this=target, to=exp.DataType.build("VARCHAR")))
        needle = exp.Lower(this=literals[0])
        return exp.Anonymous(this=TEXT_MATCH[op], expressions=[haystack, needle]), is_measure

    def _dimension_or_problem(self, path: str, source: Source, dim: Dimension) -> exp.Expr | None:
        try:
            return self.dimension_sql(source, dim)
        except ExpressionError as exc:
            self.problems.add(path, f"Dimension {dim.name!r} has an invalid expression: {exc}")
            return None

    def _measure_or_problem(self, path: str, source: Source, measure: Measure) -> exp.Expr | None:
        try:
            return self.measure_sql(source, measure)
        except ExpressionError as exc:
            self.problems.add(path, f"Measure {measure.name!r} has an invalid expression: {exc}")
            return None

    # ---- FROM ----------------------------------------------------------------------

    def _table(self, source: Source) -> exp.Expr:
        """A dataset's snapshot, with its row-level security applied inside."""
        alias = source.model.dataset
        scan = sqlglot.parse_one("SELECT * FROM read_parquet('x')", read=DIALECT)
        literal, table = scan.find(exp.Literal), scan.find(exp.Table)
        assert isinstance(scan, exp.Select) and literal is not None and table is not None
        # The path is ours (a snapshot directory), and still goes in as a literal.
        literal.replace(exp.Literal.string(f"{source.table.path}/part-*.parquet"))
        table.set("alias", exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
        conditions = []
        for index, rule in enumerate(source.model.rls):
            if self.user.role not in rule.roles and self.user.may_be_exempt:
                self.rls_bypassed = True  # reported to the user, never silent
                continue
            try:
                conditions.append(
                    expressions.rls_expression(
                        rule.filter, alias, list(source.columns), self.user.values()
                    )
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
        return scan.subquery(exp.to_identifier(alias, quoted=True))

    # ---- the whole query -----------------------------------------------------------

    def compile(
        self, max_rows: int, timeout_s: float
    ) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
        query = self.query
        projections: list[exp.Expr] = []
        columns: list[ResultColumn] = []
        group_count = 0

        def select(ref: str, expression: exp.Expr, column: ResultColumn) -> None:
            if any(c.name == ref for c in columns):
                self.problems.add("/", f"{ref!r} is selected more than once.")
                return
            projections.append(exp.alias_(expression, ref, quoted=True))
            columns.append(column)

        for index, ref in enumerate(query.get("dimensions") or []):
            found = self.dimension(f"/dimensions/{index}", ref)
            if found is None:
                continue
            source, dim = found
            sql = self._dimension_or_problem(f"/dimensions/{index}", source, dim)
            if sql is not None:
                select(
                    ref,
                    sql,
                    ResultColumn(ref, _dimension_type(source, dim), "dimension", label=dim.label),
                )
                group_count += 1

        where: list[exp.Expr] = []
        time_spec = query.get("timeDimension")
        if time_spec:
            ref = str(time_spec["field"])
            found = self.dimension("/timeDimension/field", ref)
            if found is not None:
                source, dim = found
                grain = time_spec.get("grain")
                sql = self._dimension_or_problem("/timeDimension/field", source, dim)
                if dim.type != "time":
                    self.problems.add("/timeDimension/field", f"{ref!r} is not a time dimension.")
                elif sql is not None:
                    if grain and grain not in dim.grains:
                        self.problems.add(
                            "/timeDimension/grain",
                            f"{ref!r} allows these grains: {', '.join(dim.grains)}.",
                        )
                    elif grain:
                        select(
                            ref,
                            grain_expression(sql, grain, self.calendar),
                            ResultColumn(
                                ref, ColumnType.DATE, "time", label=dim.label, grain=grain
                            ),
                        )
                        group_count += 1
                    if "range" in time_spec:
                        where.extend(self._range("/timeDimension/range", sql, time_spec["range"]))

        for index, ref in enumerate(query.get("measures") or []):
            found_measure = self.measure(f"/measures/{index}", ref)
            if found_measure is None:
                continue
            source, measure = found_measure
            sql = self._measure_or_problem(f"/measures/{index}", source, measure)
            if sql is not None:
                select(
                    ref,
                    sql,
                    ResultColumn(
                        ref,
                        _measure_type(source, measure),
                        "measure",
                        format=measure.format,
                        label=measure.label,
                    ),
                )

        having: list[exp.Expr] = []
        for index, spec in enumerate(query.get("filters") or []):
            condition, is_measure = self._filter(index, spec)
            if condition is not None:
                (having if is_measure else where).append(condition)

        for index, ref in enumerate(query.get("segments") or []):
            path = f"/segments/{index}"
            found_source = self._source(path, ref)
            if found_source is None:
                continue
            source, name = found_source
            segment = source.model.segments.get(name)
            if segment is None:
                self.problems.add(path, f"There is no segment called {ref!r}.")
                continue
            try:
                where.append(
                    exp.Paren(
                        this=expressions.column_expression(
                            segment.filter, source.model.dataset, list(source.columns)
                        )
                    )
                )
            except ExpressionError as exc:
                self.problems.add(path, f"Segment {name!r} has an invalid filter: {exc}")

        selected = {c.name for c in columns}
        order: list[exp.Expr] = []
        for index, spec in enumerate(query.get("sort") or []):
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

        if not projections and not self.problems.items:
            self.problems.add("/", "Select at least one dimension, measure or time grain.")
        self.problems.raise_if_any()

        limit = min(int(query.get("limit") or DEFAULT_LIMIT), max_rows)
        statement = exp.select(*projections).from_(self._table(self.base))
        for name in self.used[1:]:
            join = next(j for j in self.base.model.joins if j.to == name)
            condition = exp.and_(
                *[
                    exp.EQ(this=_col(self.base.model.dataset, local), expression=_col(name, remote))
                    for local, remote in join.on
                ]
            )
            statement = statement.join(
                self._table(self.sources[name]), on=condition, join_type=join.type
            )
        if where:
            statement = statement.where(exp.and_(*where))
        if group_count:
            statement = statement.group_by(*[exp.Literal.number(i + 1) for i in range(group_count)])
        if having:
            statement = statement.having(exp.and_(*having))
        if order:
            statement = statement.order_by(*order)
        # One extra row tells the engine whether the result was cut off.
        statement = statement.limit(limit + 1)
        if query.get("offset"):
            statement = statement.offset(int(query["offset"]))

        text = statement.sql(dialect=DIALECT, comments=False)
        tables = tuple(self.sources[name].table for name in self.used)
        compiled = CompiledQuery(text, (), tables, limit, timeout_s, self.rls_bypassed)
        return compiled, tuple(columns)


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


def compile_query(
    query: Mapping[str, Any],
    sources: Mapping[str, Source],
    user: UserContext,
    calendar: CalendarConfig,
    today: date,
    *,
    max_rows: int,
    timeout_s: float,
) -> tuple[CompiledQuery, tuple[ResultColumn, ...]]:
    """Compile a schema-valid semantic query. Raises ApiError(422) with paths on problems."""
    return _Compiler(query, sources, user, calendar, today).compile(max_rows, timeout_s)
