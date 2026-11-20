"""The dashboard spec: validation, and deriving each item's semantic query.

Validation is two-phase (ADR-004). The JSON Schema checks structure. This
module then checks what a schema cannot: ids are unique, visuals exist,
bindings satisfy the manifest's roles, config matches the manifest's
configSchema, fields exist in the semantic models, and items fit the grid.
Every problem carries a JSON pointer path, so a person or an agent can fix it.

Items never contain queries. `item_query` derives one from the bindings plus
the dashboard's filters, which keeps specs small and hard to get wrong.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from opsis.dashboards.manifests import Manifest
from opsis.semantic.calendar import CalendarConfig, resolve_relative_range
from opsis.semantic.model import (
    SAFE_JOINS,
    Dimension,
    Measure,
    Model,
    Problem,
    schema_problems,
    schema_validator,
)

DEFAULT_LIMIT = 1000
Field = tuple[str, Dimension | Measure]  # ("dimension" | "measure", definition)


def spec_datasets(spec: Mapping[str, Any]) -> set[str]:
    """Every dataset a spec reads: item datasets, bound fields and filter fields."""
    names: set[str] = set()
    for item in spec.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("dataset"), str):
            names.add(item["dataset"])
        for entries in (item.get("bindings") or {}).values():
            for entry in entries if isinstance(entries, list) else []:
                ref = (
                    entry
                    if isinstance(entry, str)
                    else entry.get("field")
                    if isinstance(entry, Mapping)
                    else None
                )
                if isinstance(ref, str):
                    names.add(ref.partition(".")[0])
    for control in spec.get("filters") or []:
        if isinstance(control, Mapping) and isinstance(control.get("field"), str):
            names.add(control["field"].partition(".")[0])
    return names


def spec_fields(spec: Mapping[str, Any]) -> set[str]:
    """Every field a spec names: bound fields and filter fields. A dashboard only
    answers questions about these, so cross-filters are limited to them."""
    fields: set[str] = set()
    for item in spec.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        for entries in (item.get("bindings") or {}).values():
            for entry in entries if isinstance(entries, list) else []:
                ref = (
                    entry
                    if isinstance(entry, str)
                    else entry.get("field")
                    if isinstance(entry, Mapping)
                    else None
                )
                if isinstance(ref, str):
                    fields.add(ref)
    for control in spec.get("filters") or []:
        if isinstance(control, Mapping) and isinstance(control.get("field"), str):
            fields.add(control["field"])
    return fields


def blank_spec(title: str) -> dict[str, Any]:
    return {
        "specVersion": 1,
        "title": title,
        "layout": {"columns": 12, "rowHeight": 40},
        "filters": [],
        "items": [],
        "interactions": {"crossFilter": True},
    }


def resolve_field(models: Mapping[str, Model], base: str | None, ref: str) -> Field | None:
    """Look up "dataset.field". With `base`, the field must be reachable from that
    dataset: its own dimension or measure, or a dimension across a safe join."""
    dataset, _, name = ref.partition(".")
    model = models.get(dataset)
    if model is None:
        return None
    if base is not None and dataset != base:
        origin = models.get(base)
        join = next((j for j in origin.joins if j.to == dataset), None) if origin else None
        if join is None or join.relationship not in SAFE_JOINS:
            return None
        return ("dimension", model.dimensions[name]) if name in model.dimensions else None
    if name in model.dimensions:
        return "dimension", model.dimensions[name]
    if name in model.measures:
        return "measure", model.measures[name]
    return None


def _binding(entry: Any) -> tuple[str, str | None]:
    return (entry, None) if isinstance(entry, str) else (entry["field"], entry.get("grain"))


def validate(
    spec_dir: Path,
    spec: Any,
    manifests: Mapping[str, Manifest],
    models: Mapping[str, Model],
) -> list[Problem]:
    """Every problem in a dashboard spec. An empty list means it can be saved."""
    problems = schema_problems(schema_validator(spec_dir, "dashboard.schema.json"), spec)
    if problems:
        return problems  # the checks below assume the structure is right

    def add(path: str, message: str) -> None:
        problems.append({"path": path, "message": message})

    columns = spec["layout"]["columns"]
    seen: dict[str, str] = {}
    item_ids = {item["id"] for item in spec["items"]}

    for index, control in enumerate(spec.get("filters") or []):
        path = f"/filters/{index}"
        if control["id"] in seen:
            add(f"{path}/id", f"The id {control['id']!r} is already used at {seen[control['id']]}.")
        seen[control["id"]] = path
        found = resolve_field(models, None, control["field"])
        if found is None or found[0] != "dimension":
            add(f"{path}/field", f"{control['field']!r} is not a dimension of any dataset.")
        else:
            kind = found[1].type  # type: ignore[union-attr]
            if control["type"] == "dateRange" and kind != "time":
                add(f"{path}/type", "A dateRange filter needs a time dimension.")
            if control["type"] == "search" and kind != "string":
                add(f"{path}/type", "A search filter needs a text dimension.")
            if control["type"] in ("select", "multiSelect") and kind == "time":
                add(f"{path}/type", "Use a dateRange filter for a time dimension.")
        for position, target in enumerate(control.get("targets") or []):
            if target not in item_ids:
                add(f"{path}/targets/{position}", f"There is no item with the id {target!r}.")

    for index, item in enumerate(spec["items"]):
        path = f"/items/{index}"
        if item["id"] in seen:
            add(f"{path}/id", f"The id {item['id']!r} is already used at {seen[item['id']]}.")
        seen[item["id"]] = path
        position = item["position"]
        if position["x"] + position["w"] > columns:
            add(
                f"{path}/position",
                f"The item ends at column {position['x'] + position['w']}, "
                f"but the grid has {columns} columns.",
            )
        manifest = manifests.get(item["visual"])
        if manifest is None:
            known = ", ".join(sorted(manifests))
            add(
                f"{path}/visual",
                f"There is no visual called {item['visual']!r}. Use one of: {known}.",
            )
            continue
        _check_item(add, path, item, manifest, models)
    return problems


def _unknown_field(models: Mapping[str, Model], dataset: str, ref: str, kind: str) -> str:
    """Why `ref` cannot be used, with the closest real names. People and agents
    both fix a wrong name fastest when they are shown the right ones."""
    model = models[dataset]
    measures = [f"{dataset}.{name}" for name in model.measures]
    dimensions = [f"{dataset}.{name}" for name in model.dimensions]
    for join in model.joins:
        if join.relationship in SAFE_JOINS and join.to in models:
            dimensions += [f"{join.to}.{name}" for name in models[join.to].dimensions]
    known = (
        measures
        if kind == "measure"
        else dimensions
        if kind == "dimension"
        else measures + dimensions
    )
    message = f"{ref!r} is not a field that {dataset!r} can use."
    if "." not in ref:
        message += f" Write fields as dataset.field, for example {dataset}.{ref}."
    close = difflib.get_close_matches(ref, measures + dimensions, n=3, cutoff=0.6)
    if close:
        message += f" Did you mean {' or '.join(repr(c) for c in close)}?"
    what = {"measure": "Measures", "dimension": "Dimensions"}.get(kind, "Fields")
    shown = ", ".join(known[:20]) + (", ..." if len(known) > 20 else "")
    return f"{message} {what} available: {shown or 'none'}."


def _check_item(
    add: Any, path: str, item: Mapping[str, Any], manifest: Manifest, models: Mapping[str, Model]
) -> None:
    for error in Draft202012Validator(manifest["configSchema"]).iter_errors(
        item.get("config") or {}
    ):
        pointer = "/".join(str(p) for p in error.absolute_path)
        add(f"{path}/config{'/' + pointer if pointer else ''}", error.message[:300])

    roles: Mapping[str, Any] = manifest["roles"]
    bindings: Mapping[str, list[Any]] = item.get("bindings") or {}
    dataset = item.get("dataset")
    if not roles:
        if bindings:
            add(f"{path}/bindings", f"The {manifest['id']} visual has no data fields.")
        return
    if dataset is None:
        add(path, f"The {manifest['id']} visual needs a `dataset`.")
        return
    if dataset not in models:
        add(f"{path}/dataset", f"There is no dataset called {dataset!r} with a model and data.")
        return

    for role in bindings:
        if role not in roles:
            add(
                f"{path}/bindings/{role}",
                f"The {manifest['id']} visual has no role called {role!r}. "
                f"Its roles are: {', '.join(roles)}.",
            )
    bound: set[str] = set()
    grains = 0
    for role, rule in roles.items():
        entries = bindings.get(role) or []
        if not rule["min"] <= len(entries) <= rule["max"]:
            wanted = (
                f"exactly {rule['min']}"
                if rule["min"] == rule["max"]
                else f"{rule['min']} to {rule['max']}"
            )
            add(
                f"{path}/bindings/{role}",
                f"{rule.get('label', role)} needs {wanted} field(s), but has {len(entries)}.",
            )
        for position, entry in enumerate(entries):
            ref, grain = _binding(entry)
            where = f"{path}/bindings/{role}/{position}"
            if isinstance(entry, dict) and "axis" in entry and not rule.get("axis"):
                add(
                    f"{where}/axis",
                    f"{rule.get('label', role)} of the {manifest['id']} visual has no axis choice. "
                    "Only the combo chart's bars and lines can use the right-hand axis. "
                    "Remove `axis`.",
                )
            found = resolve_field(models, dataset, ref)
            if found is None:
                add(where, _unknown_field(models, dataset, ref, rule["kind"]))
                continue
            kind, definition = found
            bound.add(ref)
            if rule["kind"] != "any" and rule["kind"] != kind:
                add(
                    where,
                    f"{ref!r} is a {kind}, but {rule.get('label', role)} needs a {rule['kind']}.",
                )
            if kind == "dimension":
                assert isinstance(definition, Dimension)
                allowed = rule.get("types")
                if allowed and definition.type not in allowed:
                    add(
                        where,
                        f"{ref!r} is {definition.type}; this role needs {' or '.join(allowed)}.",
                    )
                if grain is not None:
                    grains += 1
                    if definition.type != "time":
                        add(f"{where}/grain", "Only a time dimension can have a grain.")
                    elif grain not in definition.grains:
                        add(f"{where}/grain", f"{ref!r} allows: {', '.join(definition.grains)}.")
            elif grain is not None:
                add(f"{where}/grain", "A measure cannot have a grain.")
    if grains > 1:
        add(f"{path}/bindings", "Only one field in an item can have a time grain.")

    for position, condition in enumerate(item.get("filters") or []):
        if resolve_field(models, dataset, condition["field"]) is None:
            add(
                f"{path}/filters/{position}/field",
                f"{condition['field']!r} is not a field that {dataset!r} can use.",
            )
    for position, ref in enumerate(item.get("segments") or []):
        owner, _, name = ref.partition(".")
        if owner not in models or name not in models[owner].segments:
            add(f"{path}/segments/{position}", f"There is no segment called {ref!r}.")
    for position, order in enumerate(item.get("sort") or []):
        if order["field"] not in bound:
            add(f"{path}/sort/{position}/field", "Sort by a field that is bound to this item.")


# ---- deriving queries ---------------------------------------------------------------


def _reachable(models: Mapping[str, Model], dataset: str, ref: str) -> bool:
    return resolve_field(models, dataset, ref) is not None


def filter_condition(control: Mapping[str, Any], value: Any) -> dict[str, Any] | None:
    """The semantic filter for one dashboard filter control and its current value."""
    field, kind = control["field"], control["type"]
    if value is None or value == "" or value == []:
        return None
    if kind == "dateRange":
        if not isinstance(value, dict) or not value or value.get("relative") == "all_time":
            return None
        return {"field": field, "op": "in_range", "range": value}
    if kind == "multiSelect":
        values = value if isinstance(value, list) else [value]
        return {"field": field, "op": "in", "values": values}
    if kind == "select":
        return {"field": field, "op": "eq", "values": [value]}
    return {"field": field, "op": "contains", "values": [str(value)]}


def item_query(
    spec: Mapping[str, Any],
    item: Mapping[str, Any],
    manifest: Manifest,
    models: Mapping[str, Model],
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The semantic query for one item, or None for a visual with no data.

    `state` is what the viewer has done: {"filters": {filter id: value},
    "cross": [{"field", "values", "source"}], "grains": {item id: grain}}.
    A filter with no entry in the state uses its default from the spec.
    """
    state = state or {}
    dataset = item.get("dataset")
    if not manifest["roles"] or dataset is None:
        return None
    query: dict[str, Any] = {"dataset": dataset}
    dimensions: list[str] = []
    measures: list[str] = []
    for role in manifest["roles"]:
        for entry in (item.get("bindings") or {}).get(role) or []:
            ref, grain = _binding(entry)
            found = resolve_field(models, dataset, ref)
            if found is not None and found[0] == "measure":
                measures.append(ref)
            elif grain is not None and "timeDimension" not in query:
                drilled = (state.get("grains") or {}).get(item["id"])
                query["timeDimension"] = {"field": ref, "grain": drilled or grain}
            else:
                dimensions.append(ref)
    if not dimensions and not measures and "timeDimension" not in query:
        return None
    if dimensions:
        query["dimensions"] = list(dict.fromkeys(dimensions))
    if measures:
        query["measures"] = list(dict.fromkeys(measures))

    conditions = [dict(c) for c in item.get("filters") or []]
    values: Mapping[str, Any] = state.get("filters") or {}
    for control in spec.get("filters") or []:
        targets = control.get("targets")
        if targets is not None and item["id"] not in targets:
            continue
        if not _reachable(models, dataset, control["field"]):
            continue  # this item's dataset has no path to the filter's field
        value = values[control["id"]] if control["id"] in values else control.get("default")
        condition = filter_condition(control, value)
        if condition is not None:
            conditions.append(condition)
    if (spec.get("interactions") or {}).get("crossFilter"):
        for cross in state.get("cross") or []:
            if cross.get("source") == item["id"] or not cross.get("values"):
                continue
            if _reachable(models, dataset, cross["field"]):
                conditions.append({"field": cross["field"], "op": "in", "values": cross["values"]})
    if conditions:
        query["filters"] = conditions
    if item.get("segments"):
        query["segments"] = list(item["segments"])
    # While editing, a sort can briefly outlive the field it names. Saving rejects
    # that; a live preview just ignores it.
    selected = {*dimensions, *measures, query.get("timeDimension", {}).get("field")}
    sort = [dict(s) for s in item.get("sort") or [] if s["field"] in selected]
    if sort:
        query["sort"] = sort
    query["limit"] = item.get("limit") or DEFAULT_LIMIT
    return query


def comparison_query(
    query: Mapping[str, Any], mode: str, calendar: CalendarConfig, today: date
) -> tuple[dict[str, Any], dict[str, str]] | None:
    """The same query over the period before, for a KPI's comparison.

    Uses the first date range among the query's filters. previous_period is
    the same number of days immediately before; previous_year is the same
    dates a year earlier. Returns None when there is no bounded range.
    """
    filters = list(query.get("filters") or [])
    index = next((i for i, f in enumerate(filters) if f.get("op") == "in_range"), None)
    if index is None or mode not in ("previous_period", "previous_year"):
        return None
    spec = filters[index]["range"]
    if "relative" in spec:
        start, end = resolve_relative_range(spec["relative"], today, calendar)
    else:
        try:
            start = date.fromisoformat(spec["from"]) if "from" in spec else None
            end = date.fromisoformat(spec["to"]) if "to" in spec else None
        except ValueError:
            return None
    if start is None or end is None or start > end:
        return None
    if mode == "previous_period":
        length = (end - start).days + 1
        before = (start - timedelta(days=length), start - timedelta(days=1))
    else:
        before = (_year_before(start), _year_before(end))
    period = {"from": before[0].isoformat(), "to": before[1].isoformat()}
    filters[index] = {**filters[index], "range": period}
    return {**query, "filters": filters}, period


def _year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)
