"""Agent tools: definitions and implementations.

Written to be used reliably by small models as well as large ones:

- Datasets are named, never referred to by id. Fields are always "dataset.field".
- Every description says when to use the tool and shows one example input.
- Arguments are checked before anything runs, and a wrong argument is reported
  with its path and what was expected.
- Every failure is returned as text with a stable code, the problems by JSON
  path, and a "What to do" line naming the next call to make.
- Results are compact JSON with capped lists.

Nothing here knows about MCP. `call` is the single entry point; the MCP
server and, later, the in-app assistant both use it.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlglot import exp

from opsis.auth.principal import Permission, Principal
from opsis.dashboards import patch as patches
from opsis.db.models import Dashboard, Dataset
from opsis.errors import ApiError
from opsis.query.engine import CompiledQuery
from opsis.semantic import model as semantic
from opsis.services import (
    audit,
    connections,
    dashboards,
    datasets,
    models,
)
from opsis.services import query as queries
from opsis.services.query import QueryRuntime
from opsis.settings import Settings
from opsis.storage import snapshots

MAX_QUERY_ROWS = 1000
MAX_PREVIEW_ROWS = 100
SAMPLE_ROWS = 200
SAMPLE_VALUES = 5
TOP_VALUES = 10
SCHEMAS = {
    "dashboard": "dashboard.schema.json",
    "semantic-query": "semantic-query.schema.json",
    "semantic-model": "semantic-model.schema.json",
    "visual-manifest": "visual-manifest.schema.json",
}


@dataclass
class ToolContext:
    """What a tool call runs with: who is calling and the app's shared machinery."""

    settings: Settings
    session_factory: sessionmaker[Session]
    runtime: QueryRuntime
    principal: Principal
    # "agent" for MCP and the assistant. Recorded on every version the call creates.
    author_type: str = "agent"


Handler = Callable[[ToolContext, Session, dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    scope: str  # read | edit | publish | admin
    permission: Permission
    writes: bool = False
    # Name of a settings flag that must be on for the tool to exist at all.
    feature: str | None = None
    hints: Mapping[str, str] = field(default_factory=dict)


class ToolFailure(Exception):
    """A tool call that did not succeed. `text` is what the model reads."""

    def __init__(self, code: str, text: str) -> None:
        self.code = code
        self.text = text
        super().__init__(text)


# ---- errors a model can act on ----------------------------------------------------

GENERAL_HINTS = {
    "version_conflict": (
        "Someone else saved in between. Read the latest version (get_dashboard or "
        "get_semantic_model), apply your change to that, and call again with base_version set "
        "to the current version number given above."
    ),
    "invalid_spec": (
        "Nothing was saved. Fix each problem at its path and call again. describe_dataset "
        "lists the valid field names; list_visuals lists each visual's roles and config options."
    ),
    "invalid_patch": (
        "Nothing was saved. Read the current document first (get_dashboard or "
        "get_semantic_model) and build paths from what is really there. To append to a list "
        'use a path ending in "/-", for example "/items/-".'
    ),
    "invalid_model": (
        "Nothing was saved. Fix each problem at its path and call again. describe_dataset "
        "lists the dataset's columns."
    ),
    "invalid_query": (
        "Fix each problem at its path. Field names are written dataset.field, exactly as "
        "describe_dataset returns them."
    ),
    "query_failed": "Check the field names with describe_dataset and simplify the query.",
    "no_data": (
        "The dataset has not been loaded yet. Call trigger_refresh for it, then get_job_status "
        "until the job has succeeded."
    ),
    "forbidden": (
        "This cannot be done with the current access token or role. Tell the user; do not retry."
    ),
    "busy": "Wait a few seconds and call again.",
    "query_timeout": "Narrow the query: add a date range or filters, or use fewer dimensions.",
}


def _fail(
    code: str, message: str, details: list[dict[str, Any]] | None = None, hint: str = ""
) -> ToolFailure:
    lines = [f"Error ({code}): {message}"]
    shown = (details or [])[:25]
    if shown:
        lines.append("Problems:")
        lines += [f"- {d.get('path') or '/'}: {d.get('message', '')}" for d in shown]
        if len(details or []) > len(shown):
            lines.append(f"- ... and {len(details or []) - len(shown)} more.")
    advice = hint or GENERAL_HINTS.get(code, "")
    if advice:
        lines.append(f"What to do: {advice}")
    return ToolFailure(code, "\n".join(lines))


def _suggest(wanted: str, known: list[str], what: str, lister: str) -> str:
    close = difflib.get_close_matches(wanted, known, n=3, cutoff=0.5)
    listed = ", ".join(known[:30]) + (", ..." if len(known) > 30 else "")
    parts = []
    if close:
        parts.append(f"Did you mean {' or '.join(repr(c) for c in close)}?")
    parts.append(f"Existing {what}: {listed}." if known else f"There are no {what} yet.")
    parts.append(f"Call {lister} to see them all.")
    return " ".join(parts)


# ---- lookups ----------------------------------------------------------------------


def _dataset(db: Session, name: Any) -> Dataset:
    found = db.scalar(select(Dataset).where(Dataset.name == str(name).strip()))
    if found is None:
        known = list(db.scalars(select(Dataset.name).order_by(Dataset.name)).all())
        raise _fail(
            "not_found",
            f"There is no dataset called {str(name)!r}.",
            hint=_suggest(str(name), known, "datasets", "list_datasets"),
        )
    return found


def _dashboard(db: Session, ref: Any) -> Dashboard:
    """A dashboard by id. A title is accepted too when exactly one dashboard has it."""
    text = str(ref).strip()
    try:
        found = db.get(Dashboard, uuid.UUID(text))
    except ValueError:
        matches = db.scalars(
            select(Dashboard).where(func.lower(Dashboard.title) == text.lower())
        ).all()
        if len(matches) > 1:
            raise _fail(
                "ambiguous",
                f"{len(matches)} dashboards are titled {text!r}.",
                hint="Call list_dashboards and pass the `id` of the one you mean.",
            ) from None
        found = matches[0] if matches else None
    if found is None:
        titles = list(db.scalars(select(Dashboard.title).order_by(Dashboard.title)).all())
        raise _fail(
            "not_found",
            f"There is no dashboard with the id or title {text!r}.",
            hint=_suggest(text, titles, "dashboard titles", "list_dashboards")
            + " Pass the `id` from list_dashboards.",
        )
    return found


def _urls(settings: Settings, dashboard_id: Any) -> dict[str, str]:
    base = settings.public_base_url.rstrip("/")
    return {
        "viewUrl": f"{base}/dashboards/{dashboard_id}",
        "editUrl": f"{base}/dashboards/{dashboard_id}/edit",
    }


def _rls_applies(model: semantic.Model, principal: Principal) -> bool:
    """True when a row-level security rule restricts what this caller may see."""
    return any(principal.role.value in rule.roles for rule in model.rls)


def _raw_source(ctx: ToolContext, db: Session, dataset: Dataset, tool: str) -> Any:
    """The dataset's snapshot for reading rows directly, which skips the semantic
    layer. Refused when row-level security applies to the caller, because raw
    rows would show what a query hides."""
    sources = queries.load_sources(db, ctx.settings, {dataset.name})
    source = sources.get(dataset.name)
    if source is None:
        raise _fail("no_data", f"Dataset {dataset.name!r} has no data yet.")
    if _rls_applies(source.model, ctx.principal):
        raise _fail(
            "forbidden",
            f"Dataset {dataset.name!r} has row-level security rules that apply to you, so "
            f"{tool} cannot read its rows directly.",
            hint="Use run_query instead. It applies the rules and returns the rows you may see.",
        )
    return source


# ---- discovery --------------------------------------------------------------------


def list_connections(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    rows = connections.list_connections(db, ctx.settings)
    return {
        "connections": [
            {
                "name": row["name"],
                "connector": row["connectorId"],
                "datasetCount": row["datasetCount"],
            }
            for row in rows
        ],
        "note": "Connections are where data comes from. To use data, call list_datasets.",
    }


def list_datasets(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    by_name = {row["name"]: row for row in datasets.list_datasets(db)}
    result = []
    for entry in models.list_models(db):
        row = by_name.get(entry["dataset"], {})
        snapshot = row.get("snapshot") or {}
        result.append(
            {
                "dataset": entry["dataset"],
                "label": entry["label"],
                "hasData": entry["hasData"],
                "rowCount": snapshot.get("rowCount"),
                "refreshedAt": snapshot.get("createdAt"),
                "dimensions": entry["dimensions"],
                "measures": entry["measures"],
                "joinsTo": entry["joins"],
            }
        )
    return {
        "datasets": result,
        "next": "Call describe_dataset with a dataset name to get its fields before building.",
    }


def _sample_values(ctx: ToolContext, source: Any) -> dict[str, list[Any]]:
    rows = snapshots.preview(_path(source), SAMPLE_ROWS)
    names = list(source.columns)
    samples: dict[str, list[Any]] = {}
    for position, name in enumerate(names):
        seen: list[Any] = []
        for row in rows:
            value = row[position] if position < len(row) else None
            if value is None or value in seen:
                continue
            seen.append(value if not isinstance(value, str) else value[:60])
            if len(seen) == SAMPLE_VALUES:
                break
        samples[name] = seen
    return samples


def _path(source: Any) -> Any:
    from pathlib import Path

    return Path(source.table.path)


def describe_dataset(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    described = models.describe(db, dataset)
    detail = datasets.detail(db, dataset)
    snapshot = detail.get("snapshot") or {}
    job = detail.get("lastJob") or {}

    samples: dict[str, list[Any]] = {}
    sample_note = None
    if described["dataset"]["hasData"]:
        source = queries.load_sources(db, ctx.settings, {dataset.name}).get(dataset.name)
        if source is not None and not _rls_applies(source.model, ctx.principal):
            samples = _sample_values(ctx, source)
        elif source is not None:
            sample_note = "Sample values are hidden because row-level security applies to you."

    definition = described["definition"]
    column_of = {
        name: spec.get("column", name) if isinstance(spec, dict) else name
        for name, spec in (definition.get("dimensions") or {}).items()
    }
    dimensions, measures, segments, joined = [], [], [], []
    for item in described["fields"]:
        owner, _, name = item["ref"].partition(".")
        if item["kind"] == "dimension":
            entry: dict[str, Any] = {
                "ref": item["ref"],
                "label": item["label"],
                "type": item["type"],
            }
            if item["type"] == "time":
                entry["grains"] = item.get("grains") or []
            if owner != dataset.name:
                entry["fromDataset"] = owner
                joined.append(entry)
                continue
            values = samples.get(column_of.get(name, name))
            if values:
                entry["sampleValues"] = values
            dimensions.append(entry)
        elif item["kind"] == "measure":
            measures.append(
                {"ref": item["ref"], "label": item["label"], "format": item.get("format")}
            )
        else:
            segments.append({"ref": item["ref"], "label": item["label"]})

    result: dict[str, Any] = {
        "dataset": dataset.name,
        "label": dataset.label,
        "hasData": described["dataset"]["hasData"],
        "rowCount": snapshot.get("rowCount"),
        "refreshedAt": snapshot.get("createdAt"),
        "lastRefresh": {"status": job.get("status"), "error": job.get("error")} if job else None,
        "modelVersion": described["version"],
        "columns": described["dataset"]["columns"],
        "dimensions": dimensions,
        "measures": measures,
        "segments": segments,
        "dimensionsFromJoinedDatasets": joined,
        "usage": (
            "Use each `ref` exactly as written, in dashboard bindings and in run_query. "
            "A measure is a ready-made number (a sum, a count); a dimension groups or filters. "
            "A time dimension takes a grain, for example "
            '{"field": "' + _first_time(dimensions, dataset.name) + '", "grain": "month"}.'
        ),
    }
    if sample_note:
        result["sampleValuesNote"] = sample_note
    if not result["hasData"]:
        result["note"] = (
            "This dataset has no data yet. Call trigger_refresh, then get_job_status, before "
            "using it in a dashboard."
        )
    return result


def _first_time(dimensions: list[dict[str, Any]], dataset: str) -> str:
    return next((d["ref"] for d in dimensions if d["type"] == "time"), f"{dataset}.some_date")


def get_semantic_model(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    described = models.describe(db, dataset)
    return {
        "dataset": dataset.name,
        "version": described["version"],
        "isGeneratedStarter": described["isStarter"],
        "definition": described["definition"],
        "note": (
            "To change it, call patch_semantic_model with base_version set to this `version` "
            "and JSON Patch paths into `definition`, for example "
            '{"op": "add", "path": "/measures/avg_amount", "value": '
            '{"agg": "avg", "column": "amount", "label": "Average amount"}}.'
        ),
    }


def _config_options(manifest: Mapping[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for name, spec in (manifest["configSchema"].get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        option: dict[str, Any] = {}
        if "enum" in spec:
            option["oneOf"] = spec["enum"]
        elif "type" in spec:
            option["type"] = spec["type"]
        if "default" in spec and not (
            isinstance(spec["default"], str) and len(spec["default"]) > 80
        ):
            option["default"] = spec["default"]
        if spec.get("title"):
            option["meaning"] = spec["title"]
        options[name] = option
    return options


def _example_item(
    manifest: Mapping[str, Any], dataset: str | None, fields: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """A valid item for this visual using real fields of `dataset`, or
    placeholders when no dataset was given. None when the dataset lacks a field
    the visual needs."""
    size = manifest.get("defaultSize") or {"w": 6, "h": 6}
    item: dict[str, Any] = {
        "id": f"{manifest['id']}_1",
        "visual": manifest["id"],
        "title": f"Example {manifest['name'].lower()}",
        "position": {"x": 0, "y": 0, "w": size["w"], "h": size["h"]},
    }
    roles: Mapping[str, Any] = manifest["roles"]
    if not roles:
        if manifest["id"] == "text":
            item["config"] = {"markdown": "## Section heading\n\nA short note."}
            del item["title"]
        return item
    item["dataset"] = dataset or "<dataset>"
    used: set[str] = set()
    grain_used = False
    bindings: dict[str, list[Any]] = {}
    for role, rule in roles.items():
        if rule["min"] < 1:
            continue
        chosen: list[Any] = []
        for _ in range(rule["min"]):
            if dataset is None:
                kind = "measure" if rule["kind"] == "measure" else "dimension"
                chosen.append(f"<dataset>.<{kind}>")
                continue
            candidates = [
                f
                for f in fields
                if f["ref"] not in used
                and f["kind"]
                in (("dimension", "measure") if rule["kind"] == "any" else (rule["kind"],))
                and (f["kind"] == "measure" or not rule.get("types") or f["type"] in rule["types"])
            ]
            # A category reads best as text; a time axis as a date.
            wants_time = bool(rule.get("types")) and "time" in rule["types"]
            candidates.sort(
                key=lambda f: (
                    f["dataset"] != dataset,
                    f["kind"] == "dimension" and (f["type"] == "time") != wants_time,
                    f["kind"] == "dimension" and f["type"] not in ("string", "time"),
                )
            )
            if not candidates:
                return None
            pick = candidates[0]
            used.add(pick["ref"])
            if pick["kind"] == "dimension" and pick["type"] == "time" and not grain_used:
                grains = pick.get("grains") or []
                if manifest["id"] != "table" and grains:
                    grain_used = True
                    chosen.append(
                        {"field": pick["ref"], "grain": "month" if "month" in grains else grains[0]}
                    )
                    continue
            chosen.append(pick["ref"])
        bindings[role] = chosen
    item["bindings"] = bindings
    return item


def list_visuals(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset_name = args.get("dataset")
    fields: list[dict[str, Any]] = []
    if dataset_name:
        dataset = _dataset(db, dataset_name)
        dataset_name = dataset.name
        fields = [f for f in models.describe(db, dataset)["fields"] if f["kind"] != "segment"]
    visuals = []
    for manifest in dashboards.manifests(ctx.settings).values():
        entry: dict[str, Any] = {
            "visual": manifest["id"],
            "name": manifest["name"],
            "whenToUse": manifest.get("aiHints") or manifest["description"],
            "roles": {
                role: {
                    "needs": rule["kind"],
                    "count": str(rule["min"])
                    if rule["min"] == rule["max"]
                    else f"{rule['min']} to {rule['max']}",
                    **({"dimensionTypes": rule["types"]} if rule.get("types") else {}),
                    **(
                        {"axisChoice": 'optional {"field": ..., "axis": "right"}'}
                        if rule.get("axis")
                        else {}
                    ),
                }
                for role, rule in manifest["roles"].items()
            },
            "configOptions": _config_options(manifest),
            "defaultSize": manifest.get("defaultSize"),
            "minSize": manifest.get("minSize"),
        }
        example = _example_item(manifest, dataset_name, fields)
        if example is not None:
            entry["exampleItem"] = example
        elif dataset_name:
            entry["exampleItem"] = None
            entry["note"] = f"Dataset {dataset_name!r} does not have the fields this visual needs."
        visuals.append(entry)
    return {
        "visuals": visuals,
        "layout": (
            "The grid is 12 columns wide. position is {x, y, w, h} in grid cells: x from 0 to 11, "
            "x + w at most 12, y counts rows downward. Items must not overlap. Put KPI cards in a "
            "row at y 0, charts below them."
        ),
        "bindings": (
            "bindings maps each role to a list of field refs from describe_dataset. Every role "
            "with a count starting at 1 or more must be filled. Measures must come from the "
            "item's own dataset."
        ),
    }


def get_spec_schema(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    name = args.get("name") or "dashboard"
    path = ctx.settings.spec_dir / SCHEMAS[name]
    result: dict[str, Any] = {"name": name, "schema": json.loads(path.read_text(encoding="utf-8"))}
    if name == "dashboard":
        result["minimalExample"] = {
            "specVersion": 1,
            "title": "Gifts overview",
            "layout": {"columns": 12, "rowHeight": 40},
            "filters": [
                {
                    "id": "f_date",
                    "type": "dateRange",
                    "field": "gifts.gift_date",
                    "label": "Gift date",
                }
            ],
            "items": [
                {
                    "id": "kpi_total",
                    "visual": "kpi",
                    "title": "Total amount",
                    "position": {"x": 0, "y": 0, "w": 3, "h": 3},
                    "dataset": "gifts",
                    "bindings": {"value": ["gifts.total_amount"]},
                }
            ],
            "interactions": {"crossFilter": True},
        }
        result["note"] = (
            "The example uses made-up field names. Take real ones from describe_dataset and "
            "ready-made items from list_visuals. `$ref`s to semantic-query.schema.json resolve in "
            'get_spec_schema with name "semantic-query".'
        )
    return result


# ---- data ---------------------------------------------------------------------------


def preview_dataset(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    _raw_source(ctx, db, dataset, "preview_dataset")
    limit = max(1, min(int(args.get("limit") or 20), MAX_PREVIEW_ROWS))
    preview = datasets.preview(db, ctx.settings, dataset, limit)
    return {
        "dataset": dataset.name,
        "columns": [{"name": c["name"], "type": c["type"]} for c in preview["columns"]],
        "rows": preview["rows"],
        "rowsShown": len(preview["rows"]),
        "rowCount": preview["rowCount"],
        "note": "These are raw source columns. For totals and groupings use run_query.",
    }


def run_query(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    query = dict(args["query"])
    limit = query.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit > MAX_QUERY_ROWS:
        query["limit"] = (
            MAX_QUERY_ROWS
            if limit is None or not isinstance(limit, int)
            else min(limit, MAX_QUERY_ROWS)
        )
    body = queries.run(db, ctx.settings, ctx.runtime, ctx.principal, query, max_rows=MAX_QUERY_ROWS)
    result: dict[str, Any] = {
        "columns": [
            {"name": c["name"], "label": c["label"], "type": c["type"]} for c in body["columns"]
        ],
        "rows": body["rows"],
        "rowCount": body["rowCount"],
        "truncated": body["truncated"],
    }
    if body["truncated"]:
        result["note"] = (
            f"Only the first {body['rowCount']} rows are shown. Add filters, a date range, "
            "a sort and a smaller limit to get the rows that matter."
        )
    return result


def _profile_sql(path: str, column: str, top: bool) -> str:
    scan = sqlglot.parse_one("SELECT * FROM read_parquet('x')", read="duckdb")
    literal = scan.find(exp.Literal)
    assert isinstance(scan, exp.Select) and literal is not None
    literal.replace(exp.Literal.string(f"{path}/part-*.parquet"))
    col = exp.column(column, quoted=True)
    source = scan.subquery("t")
    if top:
        count = exp.Count(this=exp.Star())
        query = (
            exp.select(col.as_("value"), count.as_("rows"))
            .from_(source)
            .where(col.is_(exp.null()).not_())
            .group_by(col)
            .order_by(exp.Ordered(this=count.copy(), desc=True), exp.Ordered(this=col.copy()))
            .limit(TOP_VALUES)
        )
    else:
        query = exp.select(
            exp.Count(this=exp.Star()).as_("rows"),
            exp.Count(this=col.copy()).as_("non_null"),
            exp.Count(this=exp.Distinct(expressions=[col.copy()])).as_("distinct_values"),
            exp.Min(this=col.copy()).as_("min"),
            exp.Max(this=col.copy()).as_("max"),
        ).from_(source)
    return query.sql(dialect="duckdb")


def profile_column(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    source = _raw_source(ctx, db, dataset, "profile_column")
    wanted = str(args["column"]).strip()
    # Accept a field ref ("gifts.fund") for a dimension that reads one column.
    if wanted.startswith(f"{dataset.name}."):
        name = wanted.partition(".")[2]
        dimension = source.model.dimensions.get(name)
        wanted = (dimension.column if dimension and dimension.column else name) or name
    if wanted not in source.columns:
        raise _fail(
            "not_found",
            f"Dataset {dataset.name!r} has no column called {wanted!r}.",
            hint=_suggest(wanted, list(source.columns), "columns", "describe_dataset"),
        )
    db.rollback()

    def run(top: bool) -> list[list[Any]]:
        compiled = CompiledQuery(
            sql=_profile_sql(source.table.path, wanted, top),
            params=(),
            tables=(source.table,),
            row_limit=TOP_VALUES,
            timeout_s=ctx.settings.query_timeout_seconds,
        )
        table = ctx.runtime.execute(compiled, wait_seconds=ctx.settings.query_timeout_seconds).table
        columns = [column.to_pylist() for column in table.columns]
        return [[queries._json(column[i]) for column in columns] for i in range(table.num_rows)]

    rows, non_null, distinct, low, high = run(False)[0]
    return {
        "dataset": dataset.name,
        "column": wanted,
        "type": source.columns[wanted],
        "rows": rows,
        "nulls": rows - non_null,
        "distinctValues": distinct,
        "min": low,
        "max": high,
        "mostCommon": [{"value": value, "rows": count} for value, count in run(True)],
    }


# ---- modelling ----------------------------------------------------------------------


def validate_model(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    found = models.problems(db, ctx.settings, dataset, args["definition"])
    return {"valid": not found, "problems": found[:50]}


def patch_semantic_model(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    described = models.describe(db, dataset)
    base = int(args["base_version"])
    if base != described["version"]:
        raise _fail(
            "version_conflict",
            f"The model of {dataset.name!r} is at version {described['version']}, not {base}.",
            [{"path": "/base_version", "message": f"Current version is {described['version']}."}],
        )
    patched = patches.apply(described["definition"], args["ops"])
    record = models.save(
        db,
        ctx.settings,
        ctx.principal,
        dataset,
        patched,
        base_version=base,
        message=args.get("message"),
        author_type=ctx.author_type,
    )
    return {
        "dataset": dataset.name,
        "version": record.version,
        "saved": True,
        "note": "Dashboards that use this dataset pick up the change on their next load.",
    }


# ---- dashboards ---------------------------------------------------------------------


def list_dashboards(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    rows = dashboards.list_dashboards(db, ctx.principal)
    return {
        "dashboards": [
            {
                "id": row["id"],
                "title": row["title"],
                "version": row["version"],
                "publishedVersion": row["publishedVersion"],
                "updatedAt": row["updatedAt"],
            }
            for row in rows[:200]
        ]
    }


def get_dashboard(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dashboard = _dashboard(db, args["id"])
    wanted = args.get("version")
    if wanted is not None:
        if not dashboards.can_edit(ctx.principal):
            raise _fail("forbidden", "Only editors can read old versions of a dashboard.")
        spec = dashboards.get_version(db, dashboard, int(wanted))["spec"]
        version: int | None = int(wanted)
    else:
        detail = dashboards.detail(db, dashboard, ctx.principal)
        spec = detail["spec"]
        version = dashboard.version if detail["source"] == "draft" else dashboard.published_version
    return {
        "id": str(dashboard.id),
        "title": dashboard.title,
        "version": version,
        "latestVersion": dashboard.version,
        "publishedVersion": dashboard.published_version,
        "items": [
            {"index": index, "id": item["id"], "visual": item["visual"], "title": item.get("title")}
            for index, item in enumerate(spec.get("items") or [])
        ],
        "spec": spec,
        **_urls(ctx.settings, dashboard.id),
        "note": (
            f"To change it, call patch_dashboard with base_version {dashboard.version}. Paths "
            'point into `spec`: "/items/0/title" is the title of the item with index 0, '
            '"/items/-" appends a new item.'
        ),
    }


def _saved(ctx: ToolContext, db: Session, dashboard: Dashboard, what: str) -> dict[str, Any]:
    spec = dashboards.get_version(db, dashboard, dashboard.version)["spec"]
    return {
        "id": str(dashboard.id),
        "title": dashboard.title,
        "version": dashboard.version,
        "publishedVersion": dashboard.published_version,
        "itemCount": len(spec.get("items") or []),
        "saved": True,
        **_urls(ctx.settings, dashboard.id),
        "note": (
            f"{what} Use base_version {dashboard.version} for the next patch_dashboard call. "
            "It is a draft: viewers see it only after publish_dashboard. Give the user the editUrl."
        ),
    }


def create_dashboard(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    spec = args.get("spec")
    title = str(args.get("title") or (spec or {}).get("title") or "").strip()
    if not title:
        raise _fail("invalid_request", "Give the dashboard a `title`.")
    if spec is not None:
        # The explicit argument wins, so the two can never disagree.
        spec = {**spec, "title": title}
    created = dashboards.create(
        db,
        ctx.settings,
        ctx.principal,
        title=title,
        spec=spec,
        author_type=ctx.author_type,
        message=args.get("message"),
    )
    what = (
        "Created."
        if spec is not None
        else "Created an empty dashboard. Add items with patch_dashboard."
    )
    return _saved(ctx, db, created, what)


def patch_dashboard(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dashboard = _dashboard(db, args["id"])
    patched = dashboards.patch(
        db,
        ctx.settings,
        ctx.principal,
        dashboard.id,
        args["ops"],
        base_version=int(args["base_version"]),
        message=args.get("message"),
        author_type=ctx.author_type,
    )
    return _saved(ctx, db, patched, "Saved.")


def validate_spec(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    found = dashboards.problems(db, ctx.settings, args["spec"])
    result: dict[str, Any] = {"valid": not found, "problems": found[:50]}
    if found:
        result["whatToDo"] = GENERAL_HINTS["invalid_spec"]
    return result


def list_versions(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dashboard = _dashboard(db, args["id"])
    rows = dashboards.list_versions(db, dashboard)[:50]
    return {
        "id": str(dashboard.id),
        "latestVersion": dashboard.version,
        "publishedVersion": dashboard.published_version,
        "versions": [
            {
                "version": row["version"],
                "by": row["authorName"],
                "authorType": row["authorType"],
                "message": row["message"],
                "createdAt": row["createdAt"],
            }
            for row in rows
        ],
    }


def restore_version(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dashboard = _dashboard(db, args["id"])
    number = int(args["version"])
    restored = dashboards.restore(
        db, ctx.settings, ctx.principal, dashboard, number, author_type=ctx.author_type
    )
    return _saved(
        ctx, db, restored, f"Version {number} was copied into a new version; history is kept."
    )


def publish_dashboard(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dashboard = _dashboard(db, args["id"])
    version = args.get("version")
    published = dashboards.publish(
        db,
        ctx.principal,
        dashboard,
        int(version) if version is not None else None,
        author_type=ctx.author_type,
    )
    return {
        "id": str(published.id),
        "title": published.title,
        "publishedVersion": published.published_version,
        "latestVersion": published.version,
        **_urls(ctx.settings, published.id),
        "note": "Viewers now see this version. Give the user the viewUrl.",
    }


# ---- operations ---------------------------------------------------------------------


def trigger_refresh(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    dataset = _dataset(db, args["dataset"])
    job = datasets.request_refresh(db, ctx.principal, dataset, full=bool(args.get("full")))
    return {
        "jobId": str(job.id),
        "dataset": dataset.name,
        "status": job.status,
        "next": "Call get_job_status with this jobId every few seconds until it has finished.",
    }


def get_job_status(ctx: ToolContext, db: Session, args: dict[str, Any]) -> Any:
    try:
        job_id = uuid.UUID(str(args["job_id"]))
    except ValueError:
        raise _fail(
            "invalid_request",
            "`job_id` must be the jobId returned by trigger_refresh.",
        ) from None
    job = datasets.get_job(db, job_id)
    result = {
        "jobId": job["id"],
        "status": job["status"],
        "error": job["error"],
        "result": job["result"],
        "finishedAt": job["finishedAt"],
    }
    if job["status"] in ("queued", "running"):
        result["next"] = "Not finished. Call get_job_status again in a few seconds."
    return result


# ---- the catalogue --------------------------------------------------------------------

_DATASET = {
    "type": "string",
    "description": 'A dataset name from list_datasets, for example "gifts".',
}
_DASHBOARD_ID = {
    "type": "string",
    "description": "The dashboard's `id` from list_dashboards or create_dashboard.",
}
_OPS = {
    "type": "array",
    "minItems": 1,
    "maxItems": patches.MAX_OPS,
    "description": (
        "JSON Patch (RFC 6902) operations, applied in order. All apply or none do. "
        "Each is an object with `op` (add, remove, replace, move, copy or test), `path`, and "
        "`value` for add, replace and test."
    ),
    "items": {
        "type": "object",
        "required": ["op", "path"],
        "properties": {
            "op": {"enum": list(patches.OPS)},
            "path": {"type": "string"},
            "from": {"type": "string"},
            "value": {},
        },
    },
}
_MESSAGE = {
    "type": "string",
    "maxLength": 500,
    "description": "A short note for the version history saying what changed and why.",
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


EDIT = Permission.DASHBOARDS_EDIT

TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_datasets",
        "List datasets",
        "List every dataset with its row count and when it was last refreshed. Start here: "
        "dashboards and queries are built on datasets. Takes no arguments. Example input: {}",
        _schema({}),
        list_datasets,
        "read",
        EDIT,
    ),
    Tool(
        "describe_dataset",
        "Describe a dataset",
        "Get everything needed to use one dataset: its dimensions and measures with the exact "
        "`ref` to use for each (written dataset.field), sample values, row count and freshness. "
        "Always call this before create_dashboard, patch_dashboard or run_query, and copy field "
        'refs from its result. Example input: {"dataset": "gifts"}',
        _schema({"dataset": _DATASET}, ["dataset"]),
        describe_dataset,
        "read",
        EDIT,
    ),
    Tool(
        "list_visuals",
        "List visuals",
        "List the kinds of visual a dashboard can contain (KPI card, bar, line, table and so "
        "on), the roles each one binds fields to, its config options, and guidance on when to "
        "use it. Pass `dataset` to get a ready-to-use `exampleItem` for each visual built from "
        "that dataset's real fields: copy it, change the id, title and position, and put it in "
        'a dashboard. Example input: {"dataset": "gifts"}',
        _schema(
            {
                "dataset": {
                    **_DATASET,
                    "description": "Optional. A dataset name; examples then use its real fields.",
                }
            }
        ),
        list_visuals,
        "read",
        EDIT,
    ),
    Tool(
        "get_spec_schema",
        "Get a spec schema",
        "Get the JSON Schema that a dashboard spec must follow, with a minimal example. Use it "
        "when validate_spec reports a problem you do not understand, or to look up filter "
        "controls and less common options. For normal work list_visuals is shorter and enough. "
        '`name` is one of "dashboard" (default), "semantic-query", "semantic-model", '
        '"visual-manifest". Example input: {"name": "dashboard"}',
        _schema({"name": {"enum": list(SCHEMAS), "default": "dashboard"}}),
        get_spec_schema,
        "read",
        EDIT,
    ),
    Tool(
        "list_connections",
        "List connections",
        "List the data source connections (databases and uploaded files) and how many datasets "
        "each provides. Rarely needed: to work with data use list_datasets instead. Takes no "
        "arguments. Example input: {}",
        _schema({}),
        list_connections,
        "read",
        Permission.DATASETS_EDIT,
    ),
    Tool(
        "get_semantic_model",
        "Get a semantic model",
        "Get the full definition of a dataset's semantic model (dimensions, measures, segments, "
        "joins, row-level security) and its version number. Use it before patch_semantic_model. "
        "To just see which fields exist, describe_dataset is shorter. "
        'Example input: {"dataset": "gifts"}',
        _schema({"dataset": _DATASET}, ["dataset"]),
        get_semantic_model,
        "read",
        EDIT,
    ),
    Tool(
        "preview_dataset",
        "Preview a dataset",
        "Show the first rows of a dataset's raw source columns, to see what the data looks "
        "like. At most 100 rows; default 20. For totals, counts or grouped numbers use "
        'run_query instead. Example input: {"dataset": "gifts", "limit": 10}',
        _schema(
            {
                "dataset": _DATASET,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_PREVIEW_ROWS,
                    "default": 20,
                },
            },
            ["dataset"],
        ),
        preview_dataset,
        "read",
        Permission.DATASETS_EDIT,
    ),
    Tool(
        "run_query",
        "Run a query",
        "Run a semantic query and get the rows back (at most 1000). Use it to answer a "
        "question about the data, or to check what a visual will show before adding it. "
        "`query` needs `dataset` and at least one of `measures` or `dimensions`; every field is "
        "a ref from describe_dataset. Optional: `timeDimension` {field, grain, range}, "
        "`filters` [{field, op, values}] where values is a list and op is one of eq, neq, in, "
        "not_in, gt, gte, lt, lte, between, contains, starts_with, ends_with, is_null, "
        'is_not_null; `sort` [{field, dir}]; `limit`. A range is {"relative": ...} such as '
        "last_12_months, this_fiscal_year, last_fiscal_year, year_to_date, last_30_days, or "
        '{"from": "2026-01-01", "to": "2026-06-30"}. '
        'Example input: {"query": {"dataset": "gifts", "measures": ["gifts.total_amount"], '
        '"dimensions": ["gifts.fund"], "timeDimension": {"field": "gifts.gift_date", '
        '"range": {"relative": "last_fiscal_year"}}, "sort": [{"field": "gifts.total_amount", '
        '"dir": "desc"}], "limit": 10}}',
        _schema(
            {
                "query": {
                    "type": "object",
                    "description": "A semantic query. `dataset` is required.",
                    "required": ["dataset"],
                }
            },
            ["query"],
        ),
        run_query,
        "read",
        EDIT,
    ),
    Tool(
        "profile_column",
        "Profile a column",
        "Summarise one raw column of a dataset: row count, nulls, number of distinct values, "
        "minimum, maximum and the ten most common values. Use it to decide whether a column "
        "makes a good category, filter or axis. `column` is a column name from "
        'describe_dataset. Example input: {"dataset": "gifts", "column": "fund"}',
        _schema({"dataset": _DATASET, "column": {"type": "string"}}, ["dataset", "column"]),
        profile_column,
        "read",
        Permission.DATASETS_EDIT,
    ),
    Tool(
        "validate_model",
        "Validate a semantic model",
        "Check a complete semantic model definition for a dataset without saving it. Returns "
        "`valid` and a list of problems, each with the JSON path of what to fix. "
        'Example input: {"dataset": "gifts", "definition": {"dimensions": {"fund": '
        '{"column": "fund", "type": "string"}}, "measures": {"total_amount": '
        '{"agg": "sum", "column": "amount"}}}}',
        _schema({"dataset": _DATASET, "definition": {"type": "object"}}, ["dataset", "definition"]),
        validate_model,
        "read",
        EDIT,
    ),
    Tool(
        "patch_semantic_model",
        "Change a semantic model",
        "Change a dataset's semantic model with JSON Patch operations, for example to add a "
        "measure. First call get_semantic_model, then pass its `version` as `base_version`; "
        "paths point into its `definition`. The result is validated and saved as a new "
        "version, or nothing is saved and the problems are listed. If the version has moved "
        "on you get a version_conflict error: read the model again and retry. "
        'Example input: {"dataset": "gifts", "base_version": 3, "ops": [{"op": "add", '
        '"path": "/measures/avg_amount", "value": {"agg": "avg", "column": "amount", '
        '"label": "Average gift"}}], "message": "Add average gift"}',
        _schema(
            {
                "dataset": _DATASET,
                "base_version": {"type": "integer", "minimum": 0},
                "ops": _OPS,
                "message": _MESSAGE,
            },
            ["dataset", "base_version", "ops"],
        ),
        patch_semantic_model,
        "edit",
        Permission.MODELS_EDIT,
        writes=True,
    ),
    Tool(
        "list_dashboards",
        "List dashboards",
        "List the dashboards with their id, title, latest version and published version. Use "
        "it to find the `id` of a dashboard the user names. Takes no arguments. "
        "Example input: {}",
        _schema({}),
        list_dashboards,
        "read",
        Permission.DASHBOARDS_VIEW,
    ),
    Tool(
        "get_dashboard",
        "Get a dashboard",
        "Get a dashboard's full spec and its current version number. Always call this before "
        "patch_dashboard: you need the version for `base_version` and the spec to build "
        "correct paths. The result lists each item with its index for use in paths. "
        'Example input: {"id": "3f2b6c1e-8d4a-4f0b-9c7e-2a1d5e6f7a8b"}',
        _schema(
            {
                "id": _DASHBOARD_ID,
                "version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional. An older version to read instead of the latest.",
                },
            },
            ["id"],
        ),
        get_dashboard,
        "read",
        Permission.DASHBOARDS_VIEW,
    ),
    Tool(
        "validate_spec",
        "Validate a dashboard spec",
        "Check a complete dashboard spec without saving it. Returns `valid` and a list of "
        "problems, each with the JSON path of what to fix. create_dashboard and patch_dashboard "
        "run the same check, so this is only needed to test a draft first. "
        'Example input: {"spec": {"specVersion": 1, "title": "Gifts", "layout": {"columns": 12, '
        '"rowHeight": 40}, "filters": [], "items": [], "interactions": {"crossFilter": true}}}',
        _schema({"spec": {"type": "object"}}, ["spec"]),
        validate_spec,
        "read",
        EDIT,
    ),
    Tool(
        "create_dashboard",
        "Create a dashboard",
        "Create a new dashboard and save it as a draft. Pass a complete `spec` to create it "
        "with its visuals in one step (recommended): build each item from the `exampleItem`s "
        "that list_visuals returns for your dataset. Without `spec` you get an empty dashboard "
        "to fill with patch_dashboard. The spec is validated; if it is not valid nothing is "
        "created and each problem is listed with its path. `title` names the dashboard and "
        "replaces any title inside `spec`. Returns the new `id`, `version` and "
        "`editUrl`. Example input: "
        '{"title": "Gifts overview", "spec": {"specVersion": 1, "title": "Gifts overview", '
        '"layout": {"columns": 12, "rowHeight": 40}, "filters": [], "items": [{"id": "kpi_total", '
        '"visual": "kpi", "title": "Total amount", "position": {"x": 0, "y": 0, "w": 3, "h": 3}, '
        '"dataset": "gifts", "bindings": {"value": ["gifts.total_amount"]}}], '
        '"interactions": {"crossFilter": true}}}',
        _schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "spec": {"type": "object", "description": "Optional. A complete dashboard spec."},
                "message": _MESSAGE,
            },
            ["title"],
        ),
        create_dashboard,
        "edit",
        EDIT,
        writes=True,
    ),
    Tool(
        "patch_dashboard",
        "Change a dashboard",
        "Change an existing dashboard with JSON Patch operations and save the result as a new "
        "draft version. First call get_dashboard, then pass its `version` as `base_version`. "
        'Paths point into the spec: "/title", "/items/0/title" (item with index 0), '
        '"/items/-" (append an item), "/filters/-" (append a filter). The result is '
        "validated: if it is not valid nothing is saved and each problem is listed with its "
        "path. If someone else saved first you get a version_conflict error naming the current "
        "version: call get_dashboard again and retry with that version. "
        'Example input: {"id": "3f2b6c1e-8d4a-4f0b-9c7e-2a1d5e6f7a8b", "base_version": 2, '
        '"ops": [{"op": "add", "path": "/items/-", "value": {"id": "bar_fund", "visual": "bar", '
        '"title": "Amount by fund", "position": {"x": 0, "y": 3, "w": 6, "h": 7}, "dataset": '
        '"gifts", "bindings": {"category": ["gifts.fund"], "value": ["gifts.total_amount"]}}}], '
        '"message": "Add amount by fund"}',
        _schema(
            {
                "id": _DASHBOARD_ID,
                "base_version": {"type": "integer", "minimum": 1},
                "ops": _OPS,
                "message": _MESSAGE,
            },
            ["id", "base_version", "ops"],
        ),
        patch_dashboard,
        "edit",
        EDIT,
        writes=True,
    ),
    Tool(
        "list_versions",
        "List dashboard versions",
        "List a dashboard's saved versions, newest first, with who made each one and the note "
        "they left. Use it to find a version to restore. "
        'Example input: {"id": "3f2b6c1e-8d4a-4f0b-9c7e-2a1d5e6f7a8b"}',
        _schema({"id": _DASHBOARD_ID}, ["id"]),
        list_versions,
        "read",
        EDIT,
    ),
    Tool(
        "restore_version",
        "Restore a dashboard version",
        "Make an older version of a dashboard the current draft again. This is how to undo a "
        "change. Nothing is lost: the old version is copied into a new version on top. "
        'Example input: {"id": "3f2b6c1e-8d4a-4f0b-9c7e-2a1d5e6f7a8b", "version": 2}',
        _schema(
            {"id": _DASHBOARD_ID, "version": {"type": "integer", "minimum": 1}}, ["id", "version"]
        ),
        restore_version,
        "edit",
        EDIT,
        writes=True,
    ),
    Tool(
        "publish_dashboard",
        "Publish a dashboard",
        "Make a version of a dashboard the one that viewers see. Without `version` the latest "
        "draft is published. Only do this when the user asks for the dashboard to be published "
        "or shared. "
        'Example input: {"id": "3f2b6c1e-8d4a-4f0b-9c7e-2a1d5e6f7a8b"}',
        _schema({"id": _DASHBOARD_ID, "version": {"type": "integer", "minimum": 1}}, ["id"]),
        publish_dashboard,
        "publish",
        Permission.DASHBOARDS_PUBLISH,
        writes=True,
    ),
    Tool(
        "trigger_refresh",
        "Refresh a dataset",
        "Reload a dataset from its source so it has the latest data. Returns a `jobId`; the "
        "refresh runs in the background, so follow it with get_job_status. Set `full` to true "
        "only to rebuild an incremental dataset from scratch. "
        'Example input: {"dataset": "gifts"}',
        _schema({"dataset": _DATASET, "full": {"type": "boolean", "default": False}}, ["dataset"]),
        trigger_refresh,
        "edit",
        Permission.DATASETS_EDIT,
        writes=True,
    ),
    Tool(
        "get_job_status",
        "Get a job's status",
        "Check a background job started by trigger_refresh. `status` is queued, running, "
        "succeeded or failed; a failed job has an `error` explaining why. "
        'Example input: {"job_id": "9a1c2b3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d"}',
        _schema({"job_id": {"type": "string"}}, ["job_id"]),
        get_job_status,
        "read",
        Permission.DATASETS_EDIT,
    ),
)

_BY_NAME = {tool.name: tool for tool in TOOLS}


def available(settings: Settings, principal: Principal) -> list[Tool]:
    """The tools this caller can use. Tools of features that are off do not exist,
    and tools the caller's role or token cannot use are not offered."""
    return [
        tool
        for tool in TOOLS
        if (tool.feature is None or getattr(settings, tool.feature))
        and principal.can(tool.permission)
        and principal.has_scope(tool.scope)
    ]


# ---- calling --------------------------------------------------------------------------


def _coerce(schema: Mapping[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Forgive two common slips: an object or list sent as a JSON string, and a
    whole number sent as a string."""
    fixed = dict(args)
    for name, spec in (schema.get("properties") or {}).items():
        value = fixed.get(name)
        if not isinstance(value, str) or not isinstance(spec, dict):
            continue
        kind = spec.get("type")
        if kind in ("object", "array") and value.strip()[:1] in ("{", "["):
            with contextlib.suppress(ValueError):
                fixed[name] = json.loads(value)
        elif kind == "integer" and value.strip().lstrip("-").isdigit():
            fixed[name] = int(value.strip())
        elif kind == "boolean" and value.strip().lower() in ("true", "false"):
            fixed[name] = value.strip().lower() == "true"
    return fixed


def _check_arguments(tool: Tool, args: dict[str, Any]) -> None:
    problems = []
    allowed = list(tool.input_schema.get("properties") or {})
    for error in sorted(
        Draft202012Validator(tool.input_schema).iter_errors(args),
        key=lambda e: list(e.absolute_path),
    ):
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        message = error.message[:300]
        if error.validator == "additionalProperties":
            message += f" This tool takes: {', '.join(allowed) or 'no arguments'}."
        problems.append({"path": path, "message": message})
    if problems:
        raise _fail(
            "invalid_arguments",
            f"The arguments for {tool.name} are not right.",
            problems,
            hint="Send the arguments as in the example in the tool's description.",
        )


def _summary(args: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments reduced to what is worth keeping in the audit log."""
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value if not isinstance(value, str) else value[:200]
        elif key == "ops" and isinstance(value, list):
            summary["ops"] = [
                f"{op.get('op')} {op.get('path')}"[:120]
                for op in value[:20]
                if isinstance(op, dict)
            ]
            summary["opCount"] = len(value)
        elif isinstance(value, dict):
            summary[key] = {
                "keys": list(value)[:12],
                "items": len(value.get("items") or []) if key == "spec" else None,
            }
    return summary


def call(ctx: ToolContext, name: str, arguments: Mapping[str, Any] | None) -> Any:
    """Run a tool. Returns its JSON-ready result, or raises ToolFailure with text
    written for the model that made the call."""
    tool = _BY_NAME.get(name)
    offered = available(ctx.settings, ctx.principal)
    if tool is None or (tool.feature is not None and not getattr(ctx.settings, tool.feature)):
        raise _fail(
            "unknown_tool",
            f"There is no tool called {name!r}.",
            hint=f"The tools are: {', '.join(t.name for t in offered)}.",
        )
    if not ctx.principal.has_scope(tool.scope):
        raise _fail(
            "forbidden",
            f"This access token does not have the `{tool.scope}` scope that {tool.name} needs.",
            hint="Tell the user to create a token with that scope in Settings. Do not retry.",
        )
    if not ctx.principal.can(tool.permission):
        raise _fail(
            "forbidden",
            f"The role of this user ({ctx.principal.role}) does not allow {tool.name}.",
        )
    args = _coerce(tool.input_schema, dict(arguments or {}))
    _check_arguments(tool, args)
    with ctx.session_factory() as db:
        try:
            result = tool.handler(ctx, db, args)
            if tool.writes:
                audit.record(
                    db,
                    action=f"agent.{tool.name}",
                    actor=ctx.principal,
                    actor_type=ctx.author_type,
                    target_type="dashboard"
                    if "dashboard" in tool.name or "version" in tool.name
                    else "dataset",
                    target_id=str(result.get("id") or result.get("dataset") or "")[:100] or None,
                    detail={
                        "tool": tool.name,
                        "arguments": _summary(args),
                        "version": result.get("version") or result.get("publishedVersion"),
                        "auth": ctx.principal.auth_method,
                    },
                )
                db.commit()
            return result
        except ApiError as error:
            db.rollback()
            raise _fail(error.code, error.message, error.details) from None
