"""The semantic model: parsing, validation and starter-model generation.

A model is stored as JSON (see spec/semantic-model.schema.json) and edited as
YAML. `validate` runs the structural schema first, then checks that columns
exist, expressions pass the allow-list, and references resolve. Every problem
is reported with a JSON pointer path so a person or an agent can fix it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlglot import exp

from opsis.semantic import expressions
from opsis.semantic.expressions import ExpressionError

ALL_GRAINS = (
    "day", "week", "month", "quarter", "year", "fiscal_month", "fiscal_quarter", "fiscal_year",
)  # fmt: skip
DEFAULT_RLS_ROLES = ("viewer", "editor")
# Logical column type (connectors) to dimension type.
DIMENSION_TYPE = {
    "string": "string", "json": "string", "integer": "number", "float": "number",
    "decimal": "number", "boolean": "boolean", "date": "time", "timestamp": "time",
}  # fmt: skip
NUMERIC = {"integer", "float", "decimal"}

Problem = dict[str, str]  # {"path": "/dimensions/x/column", "message": "..."}


@dataclass(frozen=True)
class Dimension:
    name: str
    type: str
    column: str | None
    expr: str | None
    grains: tuple[str, ...]
    label: str
    # Time dimensions: the zone a naive timestamp was written in (IANA name).
    # None means the workspace timezone. Ignored for columns that hold instants.
    timezone: str | None = None
    hidden: bool = False  # left out of field lists; still usable
    description: str | None = None
    format: str | None = None
    data_category: str | None = None  # date, currency, postcode, ...


@dataclass(frozen=True)
class Measure:
    name: str
    agg: str | None
    column: str | None
    expr: str | None
    format: str | None
    label: str
    filter: str | None = None  # condition of a conditional measure
    # A relationship this measure uses instead of the active path (USERELATIONSHIP).
    relationship: str | None = None
    hidden: bool = False
    description: str | None = None


@dataclass(frozen=True)
class Segment:
    name: str
    filter: str
    label: str


@dataclass(frozen=True)
class Join:
    to: str
    type: str
    on: tuple[tuple[str, str], ...]
    relationship: str


@dataclass(frozen=True)
class RlsRule:
    filter: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Model:
    """One table of the workspace model (a v1 model: one dataset's)."""

    dataset: str  # the table's name, which is its dataset's
    label: str
    dimensions: Mapping[str, Dimension]
    measures: Mapping[str, Measure]
    segments: Mapping[str, Segment]
    joins: tuple[Join, ...]  # v1 models only: a workspace table has relationships instead
    rls: tuple[RlsRule, ...]
    hidden: bool = False
    description: str | None = None
    date_column: str | None = None  # set when this is the date table


def humanize(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def parse_model(dataset: str, definition: Mapping[str, Any]) -> Model:
    """Build a Model from a definition that has already passed validation: a v1
    model, or a table of the workspace model."""
    date_table = definition.get("dateTable")
    return Model(
        dataset=dataset,
        label=str(definition.get("label") or humanize(dataset)),
        dimensions={
            name: Dimension(
                name=name,
                type=d["type"],
                column=d.get("column"),
                expr=d.get("expr"),
                grains=tuple(d.get("grains") or ALL_GRAINS) if d["type"] == "time" else (),
                label=str(d.get("label") or humanize(name)),
                timezone=d.get("timezone"),
                hidden=bool(d.get("hidden", False)),
                description=d.get("description"),
                format=d.get("format"),
                data_category=d.get("dataCategory"),
            )
            for name, d in (definition.get("dimensions") or {}).items()
        },
        measures={
            name: Measure(
                name=name,
                agg=m.get("agg"),
                column=m.get("column"),
                expr=m.get("expr"),
                format=m.get("format"),
                label=str(m.get("label") or humanize(name)),
                filter=m.get("filter"),
                relationship=m.get("relationship"),
                hidden=bool(m.get("hidden", False)),
                description=m.get("description"),
            )
            for name, m in (definition.get("measures") or {}).items()
        },
        segments={
            name: Segment(
                name=name, filter=s["filter"], label=str(s.get("label") or humanize(name))
            )
            for name, s in (definition.get("segments") or {}).items()
        },
        joins=tuple(
            Join(
                to=j["to"],
                type=j.get("type", "left"),
                on=tuple(j["on"].items()),
                relationship=j.get("relationship", "many_to_one"),
            )
            for j in definition.get("joins") or []
        ),
        rls=tuple(
            RlsRule(filter=r["filter"], roles=tuple(r.get("applies_to_roles") or DEFAULT_RLS_ROLES))
            for r in definition.get("rls") or []
        ),
        hidden=bool(definition.get("hidden", False)),
        description=definition.get("description"),
        date_column=date_table.get("column") if isinstance(date_table, Mapping) else None,
    )


@lru_cache(maxsize=4)
def schema_registry(spec_dir: Path) -> tuple[Registry[Any], dict[str, dict[str, Any]]]:
    """Every spec schema, by file name, in a registry that resolves their references."""
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in spec_dir.glob("*.schema.json")]
    registry: Registry[Any] = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas
    )
    return registry, {s["$id"].rsplit("/", 1)[-1]: s for s in schemas}


@lru_cache(maxsize=16)
def schema_validator(spec_dir: Path, name: str) -> Draft202012Validator:
    """A validator for one of the spec schemas, with cross-file references resolved."""
    registry, schemas = schema_registry(spec_dir)
    return Draft202012Validator(schemas[name], registry=registry)


def schema_problems(validator: Draft202012Validator, document: Any) -> list[Problem]:
    problems = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        path = "/" + "/".join(str(p) for p in error.absolute_path)
        message = error.message
        if error.validator == "oneOf" and "/dimensions/" in path:
            message = "A dimension needs exactly one of `column` or `expr`."
        elif error.validator == "oneOf" and "/measures/" in path:
            message = "A measure needs either `agg` (with `column`) or `expr`, not both."
        elif len(message) > 300:
            message = message[:300] + "..."
        problems.append({"path": path, "message": message})
    return problems


@dataclass(frozen=True)
class TableContext:
    """What one table's checks need to know about the rest of the workspace model."""

    # Tables this one reaches many-to-one, with their columns: a measure's filter
    # may name their columns as table.column.
    related: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # Every other table's measures, for {table.measure} in a formula.
    other_measures: Mapping[str, Collection[str]] = field(default_factory=dict)
    # The relationships a measure may name, with the tables at their ends.
    relationships: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    v2: bool = False  # a workspace table rather than a v1 model


def table_problems(
    table: str,
    definition: Mapping[str, Any],
    columns: Mapping[str, str],
    *,
    prefix: str = "",
    context: TableContext | None = None,
) -> list[Problem]:
    """Every problem in one table's dimensions, measures, segments and rules, after
    the schema has passed. Paths start with `prefix` (`/tables/<name>` in the
    workspace model)."""
    context = context or TableContext()
    problems: list[Problem] = []
    names = list(columns)
    exact = {c.lower(): c for c in names}

    def add(path: str, message: str) -> None:
        problems.append({"path": f"{prefix}{path}", "message": message})

    def check_column(path: str, column: str) -> str | None:
        if column not in columns:
            hint = f" Did you mean {exact[column.lower()]!r}?" if column.lower() in exact else ""
            add(path, f"There is no column called {column!r}.{hint}")
            return None
        return columns[column]

    def check_expression(path: str, action: Any) -> None:
        try:
            action()
        except ExpressionError as exc:
            add(path, str(exc))

    dimensions = definition.get("dimensions") or {}
    measures = definition.get("measures") or {}
    for name, dim in dimensions.items():
        base = f"/dimensions/{name}"
        if name in measures:
            add(base, "A dimension and a measure cannot share a name.")
        if "column" in dim:
            actual = check_column(f"{base}/column", dim["column"])
            if dim["type"] == "time" and actual and DIMENSION_TYPE[actual] != "time":
                add(
                    f"{base}/type",
                    f"Column {dim['column']!r} is {actual}, not a date or timestamp.",
                )
        else:
            check_expression(
                f"{base}/expr", lambda d=dim: expressions.column_expression(d["expr"], "t", names)
            )
        if "grains" in dim and dim["type"] != "time":
            add(f"{base}/grains", "Only time dimensions have grains.")
        if "timezone" in dim:
            if dim["type"] != "time":
                add(f"{base}/timezone", "Only time dimensions have a timezone.")
            else:
                try:
                    ZoneInfo(dim["timezone"])
                except (ZoneInfoNotFoundError, ValueError):
                    add(
                        f"{base}/timezone",
                        f"{dim['timezone']!r} is not a timezone name. "
                        "Use one such as UTC or Australia/Melbourne.",
                    )

    related = {name: (name, list(cols)) for name, cols in context.related.items()}
    for name, measure in measures.items():
        base = f"/measures/{name}"
        if "relationship" in measure:
            ends = context.relationships.get(measure["relationship"])
            if "expr" in measure:
                add(
                    f"{base}/relationship",
                    "Only an aggregation can name a relationship. Name it on the measures "
                    "this formula uses.",
                )
            elif ends is None:
                add(
                    f"{base}/relationship",
                    f"There is no relationship with the id {measure['relationship']!r}.",
                )
        if "agg" in measure:
            if "filter" in measure:
                segment_filters = {
                    n: s["filter"] for n, s in (definition.get("segments") or {}).items()
                }
                check_expression(
                    f"{base}/filter",
                    lambda m=measure, s=segment_filters: expressions.filter_expression(
                        m["filter"], "t", names, s, related if context.v2 else None
                    ),
                )
            agg, column = measure["agg"], measure.get("column")
            if column is None:
                if agg != "count":
                    add(base, f"{agg} needs a `column`.")
                continue
            actual = check_column(f"{base}/column", column)
            if actual and agg in ("sum", "avg") and actual not in NUMERIC:
                add(f"{base}/column", f"{agg} needs a numeric column; {column!r} is {actual}.")
        else:
            for reference in expressions.measure_references(measure["expr"]):
                owner, dot, other = reference.partition(".")
                if not dot:
                    if reference not in measures:
                        add(f"{base}/expr", f"There is no measure called {reference!r}.")
                elif not context.v2:
                    add(
                        f"{base}/expr",
                        f"A v1 model cannot use another dataset's measure ({reference!r}).",
                    )
                elif owner == table:
                    if other not in measures:
                        add(f"{base}/expr", f"There is no measure called {other!r}.")
                elif other not in context.other_measures.get(owner, ()):
                    add(f"{base}/expr", f"There is no measure called {reference!r}.")
            check_expression(
                f"{base}/expr",
                lambda m=measure: expressions.measure_expression(m["expr"], lambda _: exp.null()),
            )
    for found in _cycle_problems(table, measures):
        add(found["path"], found["message"])

    for name, segment in (definition.get("segments") or {}).items():
        check_expression(
            f"/segments/{name}/filter",
            lambda s=segment: expressions.column_expression(s["filter"], "t", names),
        )

    for index, rule in enumerate(definition.get("rls") or []):
        check_expression(
            f"/rls/{index}/filter",
            lambda r=rule: expressions.rls_expression(r["filter"], "t", names, None),
        )
    date_table = definition.get("dateTable")
    if isinstance(date_table, Mapping):
        actual = check_column("/dateTable/column", date_table["column"])
        if actual and actual not in ("date", "timestamp"):
            add(
                "/dateTable/column",
                f"A date table's column must be a date; {date_table['column']!r} is {actual}.",
            )
    return problems


def validate(
    spec_dir: Path,
    dataset: str,
    definition: Any,
    columns: Mapping[str, str],
    other_datasets: Mapping[str, Mapping[str, str]],
) -> list[Problem]:
    """Every problem in a v1 model definition (semantic-model.schema.json, still read
    to import v1 files). An empty list means it can be saved.

    `columns` maps this dataset's column names to logical types.
    `other_datasets` maps every other dataset name to its columns, for joins.
    """
    if not isinstance(definition, dict):
        return [{"path": "/", "message": "The model must be a mapping."}]
    problems = schema_problems(schema_validator(spec_dir, "semantic-model.schema.json"), definition)
    if problems:
        return problems  # semantic checks assume the structure is right
    declared = definition.get("dataset")
    if declared is not None and declared != dataset:
        problems.append(
            {"path": "/dataset", "message": f"This model belongs to {dataset!r}, not {declared!r}."}
        )
    problems += table_problems(dataset, definition, columns)

    seen_targets: set[str] = set()
    for index, join in enumerate(definition.get("joins") or []):
        base = f"/joins/{index}"
        target = join["to"]
        if target == dataset:
            problems.append({"path": f"{base}/to", "message": "A dataset cannot join to itself."})
            continue
        if target in seen_targets:
            problems.append(
                {"path": f"{base}/to", "message": f"There is already a join to {target!r}."}
            )
        seen_targets.add(target)
        if target not in other_datasets:
            problems.append(
                {"path": f"{base}/to", "message": f"There is no dataset called {target!r}."}
            )
            continue
        for local, remote in join["on"].items():
            if local not in columns:
                exact = {c.lower(): c for c in columns}
                hint = f" Did you mean {exact[local.lower()]!r}?" if local.lower() in exact else ""
                problems.append(
                    {
                        "path": f"{base}/on/{local}",
                        "message": f"There is no column called {local!r}.{hint}",
                    }
                )
            if remote not in other_datasets[target]:
                problems.append(
                    {
                        "path": f"{base}/on/{local}",
                        "message": f"Dataset {target!r} has no column called {remote!r}.",
                    }
                )
    return problems


def _cycle_problems(table: str, measures: Mapping[str, Any]) -> list[Problem]:
    """Formulas that use themselves, directly or through other measures of this table.
    (A formula that reaches another table's measure ends there: that table's own check
    covers its formulas.)"""
    graph = {
        name: [
            r.partition(".")[2] if r.startswith(f"{table}.") else r
            for r in expressions.measure_references(m["expr"])
        ]
        for name, m in measures.items()
        if "expr" in m
    }
    graph = {name: [r for r in refs if r in measures] for name, refs in graph.items()}
    problems: list[Problem] = []
    state: dict[str, int] = {}

    def visit(name: str) -> bool:
        if state.get(name) == 1:
            return True
        if state.get(name) == 2 or name not in graph:
            return False
        state[name] = 1
        looped = any(visit(ref) for ref in graph[name])
        state[name] = 2
        return looped

    for name in graph:
        state.clear()
        if visit(name):
            problems.append(
                {
                    "path": f"/measures/{name}/expr",
                    "message": "This measure refers to itself, directly or through other measures.",
                }
            )
    return problems


def _identifier(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "field"
    if base[0].isdigit():
        base = f"f_{base}"
    base = base[:55]
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def starter_definition(
    label: str, columns: Mapping[str, str], primary_key: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A first model for a new dataset: a dimension per column, a row count,
    and a sum for each numeric column that is not an identifier."""
    taken: set[str] = set()
    dimensions: dict[str, Any] = {}
    for column, ctype in columns.items():
        name = _identifier(column, taken)
        entry: dict[str, Any] = {"column": column, "type": DIMENSION_TYPE.get(ctype, "string")}
        if column != name:
            entry["label"] = column  # keep the source's own wording, for example "Gift Date"
        dimensions[name] = entry
    measures: dict[str, Any] = {_identifier("row_count", taken): {"agg": "count", "label": "Rows"}}
    for column, ctype in columns.items():
        lowered = column.lower()
        is_key = column in primary_key or lowered == "id" or lowered.endswith(("_id", " id"))
        if ctype in NUMERIC and not is_key:
            name = _identifier(f"total_{column}", taken)
            measures[name] = {"agg": "sum", "column": column, "label": f"Total {column}"}
    return {"label": label, "dimensions": dimensions, "measures": measures}
