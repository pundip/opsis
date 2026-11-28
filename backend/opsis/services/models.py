"""Semantic models, v1: one table of the workspace model read and written as a v1
model, for the v1 routes (`/api/models/{dataset}`), the v1 MCP tools and v1 model
files. Kept for a release (V2_DESIGN 1.2).

- A table's v1 `joins` are its relationships (`workspace.table_view`): its active
  ones, and inactive ones into it as `one_to_many` joins. Saving replaces them.
- `version` is the workspace model's version: a stale `baseVersion` is rebased like
  any change to the workspace model, and refused (409) only when someone changed the
  same table or its relationships since.
- A save is refused for any problem in the table or its joins (as in v1), and for a
  problem it would make anywhere else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import Dataset, Snapshot, User, WorkspaceModelVersion
from opsis.errors import ApiError
from opsis.semantic import model as semantic
from opsis.semantic import workspace as ws
from opsis.semantic.relationships import grouping_steps, shortest_paths
from opsis.services import model_yaml
from opsis.services import workspace_model as wm
from opsis.settings import Settings

SCHEMA_BASE = "https://opsis.invalid/spec/"
NO_DATA = "This dataset has no data yet. Refresh it before defining a model."
MAX_VERSIONS_READ = 1000  # of the workspace model, to find the ones that changed a table


def dataset_by_name(db: Session, name: str) -> Dataset:
    dataset = db.scalar(select(Dataset).where(Dataset.name == name))
    if dataset is None:
        raise ApiError(404, "not_found", f"There is no dataset called {name!r}.")
    return dataset


def current_columns(db: Session, dataset: Dataset) -> dict[str, str]:
    snapshot = (
        db.get(Snapshot, dataset.current_snapshot_id) if dataset.current_snapshot_id else None
    )
    return {c["name"]: c["type"] for c in snapshot.columns} if snapshot else {}


def starter(dataset: Dataset, columns: Mapping[str, str]) -> dict[str, Any]:
    return semantic.starter_definition(dataset.label, columns, tuple(dataset.primary_key))


def to_yaml(dataset_name: str, definition: Mapping[str, Any]) -> str:
    return model_yaml.dump(
        {"dataset": dataset_name, **{k: v for k, v in definition.items() if k != "dataset"}}
    )


def from_yaml(text: str) -> dict[str, Any]:
    return model_yaml.load(text)


def resolve_input(definition: Any | None, yaml_text: str | None) -> Any:
    if (definition is None) == (yaml_text is None):
        raise ApiError(422, "invalid_request", "Send either `definition` or `yaml`.")
    return from_yaml(yaml_text) if yaml_text is not None else definition


# ---- reading ----------------------------------------------------------------------


def _fields(table: str, model: semantic.Model, related: bool) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = [
        {
            "ref": f"{table}.{d.name}",
            "kind": "dimension",
            "type": d.type,
            "label": d.label,
            "grains": list(d.grains),
            "dataset": table,
        }
        for d in model.dimensions.values()
    ]
    if not related:  # a query groups by another table's dimensions, not its measures
        fields += [
            {
                "ref": f"{table}.{m.name}",
                "kind": "measure",
                "type": "number",
                "label": m.label,
                "format": m.format,
                "dataset": table,
            }
            for m in model.measures.values()
        ]
        fields += [
            {
                "ref": f"{table}.{s.name}",
                "kind": "segment",
                "type": "boolean",
                "label": s.label,
                "dataset": table,
            }
            for s in model.segments.values()
        ]
    return fields


def fields_of(state: wm.State, allowed: wm.Visible, table: str) -> list[dict[str, Any]]:
    """The fields a query on `table` can use: its own, and the dimensions of every
    table it reaches through active many-to-one relationships by one shortest path
    (never through a restricted dataset, and none of a table the person may not use)."""
    entry = state.effective["tables"].get(table)
    if entry is None or table not in allowed:
        return []
    fields = _fields(table, semantic.parse_model(table, entry), related=False)
    restricted = {n for n, d in state.datasets.items() if d.restricted}
    relationships = ws.relationships_of(state.effective)
    reach = shortest_paths(table, grouping_steps(relationships), blocked=restricted)
    for other in sorted(reach.paths, key=lambda t: (len(reach.paths[t]), t)):
        other_entry = state.effective["tables"].get(other)
        if other_entry is None or other not in allowed or other not in state.datasets:
            continue
        fields += _fields(other, semantic.parse_model(other, other_entry), related=True)
    return fields


def describe(db: Session, principal: Principal, dataset: Dataset) -> dict[str, Any]:
    """Everything the v1 model page and the query builder need for one table."""
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    visible = ws.visible_part(state.effective, allowed)
    name = dataset.name
    columns = dict(state.datasets[name].columns) if name in state.datasets else {}
    definition = ws.table_view(visible, name) if name in visible["tables"] else {}
    saved = name in state.stored["tables"]
    return {
        "dataset": {
            "id": str(dataset.id),
            "name": name,
            "label": dataset.label,
            "hasData": bool(columns),
            "columns": [{"name": n, "type": t} for n, t in columns.items()],
        },
        "definition": definition,
        "yaml": to_yaml(name, definition),
        "fields": fields_of(state, allowed, name),
        "isStarter": not saved,
        "saved": saved,
        "version": state.version,
        "updatedAt": state.updated_at.isoformat() if saved and state.updated_at else None,
    }


def list_models(db: Session, principal: Principal) -> list[dict[str, Any]]:
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    visible = ws.visible_part(state.effective, allowed)
    result = []
    for dataset in db.scalars(select(Dataset).order_by(Dataset.name)):
        if dataset.name not in allowed:
            continue
        entry = visible["tables"].get(dataset.name) or {}
        view = ws.table_view(visible, dataset.name) if entry else {}
        saved = dataset.name in state.stored["tables"]
        result.append(
            {
                "dataset": dataset.name,
                "label": dataset.label,
                "hasData": dataset.current_snapshot_id is not None,
                "saved": saved,
                "isStarter": not saved,
                "dimensions": len(entry.get("dimensions") or {}),
                "measures": len(entry.get("measures") or {}),
                "joins": [j["to"] for j in view.get("joins") or []],
                "rlsRules": len(entry.get("rls") or []),
                "updatedAt": state.updated_at.isoformat() if saved and state.updated_at else None,
            }
        )
    return result


# ---- checking and saving ----------------------------------------------------------


@lru_cache(maxsize=4)
def validators(spec_dir: Path) -> tuple[Draft202012Validator, Draft202012Validator]:
    """The v2 table's schema, and the v1 joins' schema."""
    registry, _ = semantic.schema_registry(spec_dir)
    table = {"$ref": f"{SCHEMA_BASE}workspace-model.schema.json#/$defs/table"}
    joins = {"$ref": f"{SCHEMA_BASE}semantic-model.schema.json#/properties/joins"}
    return (
        Draft202012Validator(table, registry=registry),
        Draft202012Validator(joins, registry=registry),
    )


def structure_problems(settings: Settings, name: str, definition: Any) -> list[semantic.Problem]:
    if not isinstance(definition, dict):
        return [{"path": "/", "message": "The model must be a mapping."}]
    table, joins = validators(settings.spec_dir)
    entry = {
        "source": {"dataset": name},
        **{k: v for k, v in definition.items() if k not in ("dataset", "joins", "source")},
    }
    found = semantic.schema_problems(table, entry)
    for problem in semantic.schema_problems(joins, definition.get("joins", [])):
        found.append({**problem, "path": f"/joins{problem['path'].rstrip('/')}"})
    declared = definition.get("dataset")
    if declared is not None and declared != name:
        found.append(
            {"path": "/dataset", "message": f"This model belongs to {name!r}, not {declared!r}."}
        )
    return found


def join_problems(
    name: str, definition: Mapping[str, Any], state: wm.State, allowed: wm.Visible
) -> list[semantic.Problem]:
    """v1's checks of the joins themselves, with the same messages."""
    found: list[semantic.Problem] = []
    columns = state.datasets[name].columns
    seen: set[str] = set()
    for index, join in enumerate(definition.get("joins") or []):
        base = f"/joins/{index}"
        target = join["to"]
        if target == name:
            found.append({"path": f"{base}/to", "message": "A dataset cannot join to itself."})
            continue
        if target in seen:
            found.append(
                {"path": f"{base}/to", "message": f"There is already a join to {target!r}."}
            )
        seen.add(target)
        if target not in state.datasets or target not in allowed:
            found.append(
                {"path": f"{base}/to", "message": f"There is no dataset called {target!r}."}
            )
            continue
        for local, remote in join["on"].items():
            if local not in columns:
                exact = {c.lower(): c for c in columns}
                hint = f" Did you mean {exact[local.lower()]!r}?" if local.lower() in exact else ""
                found.append(
                    {
                        "path": f"{base}/on/{local}",
                        "message": f"There is no column called {local!r}.{hint}",
                    }
                )
            if remote not in state.datasets[target].columns:
                found.append(
                    {
                        "path": f"{base}/on/{local}",
                        "message": f"Dataset {target!r} has no column called {remote!r}.",
                    }
                )
    return found


def _v1_path(path: str, name: str, positions: Mapping[str, str]) -> str:
    """A workspace model path as a path in the table's v1 model."""
    prefix = f"/tables/{name}"
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix) :]
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "relationships" and parts[2] in positions:
        return f"/joins/{positions[parts[2]]}"
    return path


def _base(db: Session, state: wm.State, allowed: wm.Visible, version: int | None) -> dict[str, Any]:
    """The part of the model the person started from."""
    if version is None or version == state.version:
        return ws.visible_part(state.effective, allowed)
    if not 0 <= version < state.version:
        raise ApiError(
            409,
            "version_conflict",
            f"There is no version {version} to start from: the model is at version "
            f"{state.version}.",
            [{"path": "/baseVersion", "message": f"Current version is {state.version}."}],
        )
    return wm.as_seen(wm.version_document(db, version), state, allowed)


def definition_at(
    db: Session, principal: Principal, dataset: Dataset, version: int
) -> dict[str, Any]:
    """The table's v1 model as the person saw it at `version` of the workspace model,
    for a patch written against that version (409 when there is no such version)."""
    state = wm.current(db)
    base = _base(db, state, wm.visible_to(db, principal), version)
    return ws.table_view(base, dataset.name) if dataset.name in base["tables"] else {}


def _edited(
    base: Mapping[str, Any], name: str, definition: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The person's part of the model with the table replaced, and the position of
    each relationship among the table's joins."""
    document, positions = ws.apply_table_view(base, name, definition)
    return document, {rid: index for index, rid in positions.items()}


def problems(
    db: Session,
    settings: Settings,
    principal: Principal,
    dataset: Dataset,
    definition: Any,
    *,
    base_version: int | None = None,
) -> list[semantic.Problem]:
    """Every problem that stops a save, with paths in the v1 model."""
    state = wm.current(db)
    if dataset.name not in state.datasets:
        return [{"path": "/", "message": NO_DATA}]
    found = structure_problems(settings, dataset.name, definition)
    if found:
        return found
    allowed = wm.visible_to(db, principal)
    found = join_problems(dataset.name, definition, state, allowed)
    if found:
        return found
    edited, positions = _edited(_base(db, state, allowed, base_version), dataset.name, definition)
    try:
        full, _ = wm.plan(db, settings, state, allowed, document=edited, base_version=base_version)
    except ApiError as exc:
        if exc.code != "invalid_model":
            raise
        return [{**p, "path": _v1_path(p["path"], dataset.name, positions)} for p in exc.details]
    new, old = wm.check(settings, state, allowed, full)
    # As in v1, a problem the table or its joins already had stops the save too.
    mine = [p for p in old if _v1_path(p["path"], dataset.name, positions) != p["path"]]
    return [{**p, "path": _v1_path(p["path"], dataset.name, positions)} for p in [*mine, *new]]


def save(
    db: Session,
    settings: Settings,
    actor: Principal,
    dataset: Dataset,
    definition: Any,
    *,
    base_version: int | None = None,
    message: str | None = None,
    author_type: str = "user",
) -> wm.State:
    """Store the table (and its joins, as relationships) in the workspace model."""
    found = problems(db, settings, actor, dataset, definition, base_version=base_version)
    if found:
        raise ApiError(422, "invalid_model", "The model is not valid.", found)
    state = wm.current(db)
    allowed = wm.visible_to(db, actor)
    edited, positions = _edited(_base(db, state, allowed, base_version), dataset.name, definition)
    db.rollback()  # `save` locks the model and reads it again
    try:
        return wm.save(
            db,
            settings,
            actor,
            document=edited,
            base_version=base_version,
            message=message,
            author_type=author_type,
        )
    except ApiError as exc:
        if exc.code == "invalid_model":
            exc.details = [
                {**p, "path": _v1_path(p["path"], dataset.name, positions)} for p in exc.details
            ]
        raise


# ---- history ------------------------------------------------------------------------


def _views(
    db: Session, principal: Principal, dataset: Dataset
) -> tuple[wm.State, list[tuple[WorkspaceModelVersion, str | None, dict[str, Any]]]]:
    """The versions in which the table's v1 model changed, newest first, each with
    the model as it was then."""
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    rows = db.execute(
        select(WorkspaceModelVersion, User.display_name)
        .outerjoin(User, User.id == WorkspaceModelVersion.author_id)
        .order_by(WorkspaceModelVersion.version.desc())
        .limit(MAX_VERSIONS_READ)
    ).all()
    found: list[tuple[WorkspaceModelVersion, str | None, dict[str, Any]]] = []
    previous: str | None = None
    for row, author in reversed(rows):
        seen = wm.as_seen(row.definition, state, allowed)
        view = ws.table_view(seen, dataset.name) if dataset.name in seen["tables"] else {}
        written = json.dumps(view)
        if written != previous:
            found.append((row, author, view))
        previous = written
    found.reverse()
    return state, found[: wm.MAX_VERSIONS_LISTED]


def list_versions(db: Session, principal: Principal, dataset: Dataset) -> list[dict[str, Any]]:
    _, found = _views(db, principal, dataset)
    listed = [wm.version_entry(row, author) for row, author, _ in found]
    if listed:
        listed[0]["current"] = True
    return listed


def get_version(db: Session, principal: Principal, dataset: Dataset, number: int) -> dict[str, Any]:
    _, found = _views(db, principal, dataset)
    for index, (row, author, view) in enumerate(found):
        if row.version == number:
            return {
                **wm.version_entry(row, author),
                "current": index == 0,
                "definition": view,
                "yaml": to_yaml(dataset.name, view),
            }
    raise ApiError(404, "not_found", f"There is no version {number} of this model.")


def restore(
    db: Session, settings: Settings, actor: Principal, dataset: Dataset, number: int
) -> wm.State:
    """Make the table as it was in an old version current again, as a new version."""
    old = get_version(db, actor, dataset, number)
    return save(
        db,
        settings,
        actor,
        dataset,
        old["definition"],
        message=f"Restored version {number} of {dataset.name}",
    )
