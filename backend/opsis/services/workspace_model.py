"""The workspace model, stored (ADR-031, V2_DESIGN section 1.2).

- **One document** for the workspace: the tables someone wrote and the relationships
  (`workspace_model`), and a history with one immutable row per save
  (`workspace_model_versions`). A dataset with data and no table entry is a starter
  table, generated from its columns when read.
- **Who sees what:** a person reads and changes only the tables they may use and the
  relationships between them. The rest is put back as it was when they save, a
  problem in it is never described to them, and naming a table they may not use
  reads as naming one that does not exist.
- **Saving** a whole document or a JSON Patch. A stale change (an older `baseVersion`)
  is applied anyway when none of the paths it touches changed since; otherwise 409. A
  save is refused only for problems it adds: problems the stored model already has are
  reported, not blocking, so one broken table never stops work on the others.
- **The layout** of the Model view is saved beside the model and not versioned.
- **For queries,** `load_workspace` gives every table with data and its relationships;
  `opsis.semantic.security` reads each person's rows of them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from opsis.auth.principal import Principal
from opsis.dashboards import patch as patches
from opsis.db.models import Dataset, Snapshot, User, WorkspaceModel, WorkspaceModelVersion
from opsis.errors import ApiError
from opsis.query.engine import TableRef
from opsis.semantic import model as semantic
from opsis.semantic import workspace as ws
from opsis.semantic.security import Source
from opsis.semantic.workspace import TableData, Workspace
from opsis.services import access, audit, model_yaml
from opsis.settings import Settings
from opsis.storage import snapshots

MAX_VERSIONS_LISTED = 200
HIDDEN_PROBLEM = (
    "The change breaks a part of the model you may not use, so it cannot be saved. Ask "
    "someone who may use all of it to make the change."
)
NO_TABLE = "There is no table called {name!r}."
NO_DATASET = (
    "There is no dataset called {name!r} with data. Create it and refresh it before writing "
    "its table."
)


@dataclass(frozen=True)
class Visible:
    """The tables a person may use: every name but the datasets blocked to them
    (restricted, not granted to them and not theirs)."""

    blocked: frozenset[str] = frozenset()

    def __contains__(self, name: object) -> bool:
        return name not in self.blocked


ALL = Visible()


def visible_to(db: Session, principal: Principal) -> Visible:
    return Visible(frozenset(access.blocked_datasets(db, principal)))


@dataclass(frozen=True)
class State:
    """The stored workspace model, and the document people work with."""

    stored: dict[str, Any]  # the tables someone wrote, and the relationships
    version: int
    layout: dict[str, Any]
    rows: Mapping[str, tuple[Dataset, Snapshot]]  # every dataset with data
    datasets: Mapping[str, TableData]
    effective: dict[str, Any]  # stored, with a starter table for every other dataset
    generated: frozenset[str]
    updated_at: datetime | None = None


# ---- reading ----------------------------------------------------------------------


def _rows(db: Session) -> dict[str, tuple[Dataset, Snapshot]]:
    rows = db.execute(
        select(Dataset, Snapshot)
        .join(Snapshot, Snapshot.id == Dataset.current_snapshot_id)
        .order_by(Dataset.name)
    ).all()
    return {dataset.name: (dataset, snapshot) for dataset, snapshot in rows}


def _table_data(dataset: Dataset, snapshot: Snapshot) -> TableData:
    return TableData(
        dataset.name,
        dataset.label,
        {c["name"]: c["type"] for c in snapshot.columns},
        tuple(dataset.primary_key or ()),
        dataset.restricted,
    )


def _record(db: Session, *, lock: bool = False) -> WorkspaceModel | None:
    query = select(WorkspaceModel).where(WorkspaceModel.id == 1)
    if lock:
        # The migration makes the row on a workspace that had models; a new workspace
        # gets it on its first save. The lock orders saves one after another.
        db.execute(
            pg_insert(WorkspaceModel)
            .values(id=1, definition=ws.empty_document(), layout={}, version=0)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        query = query.with_for_update()
    return db.scalar(query)


def _state(record: WorkspaceModel | None, rows: Mapping[str, tuple[Dataset, Snapshot]]) -> State:
    datasets = {name: _table_data(*row) for name, row in rows.items()}
    stored = ws.definition_of(record.definition) if record else ws.empty_document()
    effective, generated = ws.effective(stored, datasets)
    return State(
        stored=stored,
        version=record.version if record else 0,
        layout=dict(record.layout or {}) if record else {},
        rows=rows,
        datasets=datasets,
        effective=effective,
        generated=frozenset(generated),
        updated_at=record.updated_at if record else None,
    )


def current(db: Session) -> State:
    return _state(_record(db), _rows(db))


def workspace_of(
    settings: Settings, state: State, document: Mapping[str, Any] | None = None
) -> Workspace:
    """Every table with data, its model and current snapshot, and the relationships
    between them: of the stored model, or of `document` (a whole effective model)."""
    full = state.effective if document is None else document
    sources: dict[str, Source] = {}
    for name, (dataset, snapshot) in state.rows.items():
        entry = full["tables"].get(name)
        if entry is None:
            continue
        path = snapshots.snapshot_dir(settings, dataset.id, snapshot.id)
        sources[name] = Source(
            model=semantic.parse_model(name, entry),
            columns=state.datasets[name].columns,
            table=TableRef(name, str(dataset.id), str(snapshot.id), str(path)),
            timezones={c["name"]: c["tz"] for c in snapshot.columns if c.get("tz")},
            restricted=dataset.restricted,
        )
    relationships = tuple(
        rel
        for rel in ws.relationships_of(full)
        if rel.from_table in sources and rel.to_table in sources
    )
    return Workspace(sources, relationships, settings.workspace_timezone)


def load_workspace(db: Session, settings: Settings) -> Workspace:
    """What queries, previews and profiles read through."""
    return workspace_of(settings, current(db))


def _hidden(path: str, allowed: Visible, document: Mapping[str, Any]) -> bool:
    """True when a problem's or warning's path is in a part of the model the person
    may not use."""
    parts = path.split("/")
    if len(parts) < 3:
        return False
    name = parts[2].replace("~1", "/").replace("~0", "~")
    if parts[1] == "tables":
        return name not in allowed
    if parts[1] == "relationships":
        rel = (document.get("relationships") or {}).get(name)
        return rel is not None and not (
            rel["from"]["table"] in allowed and rel["to"]["table"] in allowed
        )
    return False


def warning_hidden(
    warning: Mapping[str, Any], allowed: Visible, document: Mapping[str, Any]
) -> bool:
    return _hidden(warning["path"], allowed, document) or any(
        warning[key] not in allowed for key in ("table", "target") if key in warning
    )


def _redact(
    found: Iterable[semantic.Problem], allowed: Visible, document: Mapping[str, Any]
) -> list[semantic.Problem]:
    """The problems the person may read, and one that says the rest are hidden."""
    shown: list[semantic.Problem] = []
    hidden = False
    for problem in found:
        if _hidden(problem["path"], allowed, document):
            hidden = True
        elif problem not in shown:
            shown.append(problem)
    if hidden:
        shown.append({"path": "/", "message": HIDDEN_PROBLEM})
    return shown


def problems_of(
    settings: Settings, document: Mapping[str, Any], state: State, allowed: Visible
) -> list[semantic.Problem]:
    return ws.validate(settings.spec_dir, dict(document), state.datasets, allowed.blocked)


def _tables_info(state: State, document: Mapping[str, Any]) -> list[dict[str, Any]]:
    info = []
    for name, entry in document["tables"].items():
        row = state.rows.get(name)
        if row is None:  # a table whose dataset has no data (a problem says so)
            info.append(
                {
                    "name": name,
                    "label": entry.get("label") or name,
                    "hasData": False,
                    "restricted": False,
                    "primaryKey": [],
                    "columns": [],
                    "rowCount": None,
                    "generated": False,
                }
            )
            continue
        dataset, snapshot = row
        info.append(
            {
                "name": name,
                "datasetId": str(dataset.id),
                "label": entry.get("label") or dataset.label,
                "hasData": True,
                "restricted": dataset.restricted,
                "primaryKey": list(dataset.primary_key or ()),
                "columns": [{"name": c["name"], "type": c["type"]} for c in snapshot.columns],
                "rowCount": snapshot.row_count,
                "generated": name in state.generated,
            }
        )
    return info


def describe(
    db: Session, settings: Settings, principal: Principal, state: State | None = None
) -> dict[str, Any]:
    """The part of the model this person may use, as the Model view and the agent read
    it: the document (starter tables included), as YAML too, the layout, each table's
    data, and what is wrong or surprising in it (warnings that need the data are added
    by `services.model_warnings`)."""
    state = state or current(db)
    allowed = visible_to(db, principal)
    document = ws.visible_part(state.effective, allowed)
    found = problems_of(settings, state.stored, state, allowed)
    return {
        "version": state.version,
        "document": document,
        "yaml": to_yaml(document),
        "layout": visible_layout(state, allowed),
        "tables": _tables_info(state, document),
        "problems": [p for p in found if not _hidden(p["path"], allowed, state.effective)],
        "warnings": [
            w
            for w in ws.structure_warnings(state.stored, state.datasets)
            if not warning_hidden(w, allowed, state.effective)
        ],
        "updatedAt": state.updated_at.isoformat() if state.updated_at else None,
    }


def visible_layout(state: State, allowed: Visible) -> dict[str, Any]:
    layout = {k: v for k, v in state.layout.items() if k != "tables"}
    layout["tables"] = {
        name: place
        for name, place in (state.layout.get("tables") or {}).items()
        if name in allowed and name in state.effective["tables"]
    }
    return layout


# ---- YAML ---------------------------------------------------------------------------


def to_yaml(document: Mapping[str, Any]) -> str:
    return model_yaml.dump(dict(document))


def from_yaml(text: str) -> dict[str, Any]:
    return model_yaml.load(text, "a mapping with modelVersion, tables and relationships")


def resolve_input(document: Any | None, yaml_text: str | None) -> Any:
    if (document is None) == (yaml_text is None):
        raise ApiError(422, "invalid_request", "Send either `document` or `yaml`.")
    return from_yaml(yaml_text) if yaml_text is not None else document


# ---- saving -------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a) == json.dumps(b)


def schema_check(settings: Settings, document: Any) -> list[semantic.Problem]:
    if not isinstance(document, dict):
        return [{"path": "/", "message": "The workspace model must be a mapping."}]
    validator = semantic.schema_validator(settings.spec_dir, ws.SCHEMA)
    return semantic.schema_problems(validator, document)


def _invalid(found: list[semantic.Problem]) -> ApiError:
    return ApiError(422, "invalid_model", "The model is not valid.", found)


def version_document(db: Session, number: int) -> dict[str, Any]:
    if number == 0:
        return ws.empty_document()
    row = db.scalar(select(WorkspaceModelVersion).where(WorkspaceModelVersion.version == number))
    if row is None:
        raise ApiError(404, "not_found", f"There is no version {number} of the model.")
    return ws.definition_of(row.definition)


def _names_hidden(
    edited: Mapping[str, Any], state: State, allowed: Visible
) -> list[semantic.Problem]:
    """Tables and relationships of a change that name tables the person may not use,
    described as if those did not exist."""
    found: list[semantic.Problem] = []
    for name in edited["tables"]:
        if name not in allowed:
            found.append({"path": f"/tables/{name}", "message": NO_DATASET.format(name=name)})
    stored = state.effective["relationships"]
    for rid, rel in edited["relationships"].items():
        for end in ("from", "to"):
            table = rel[end]["table"]
            if table not in allowed:
                found.append(
                    {
                        "path": f"/relationships/{rid}/{end}/table",
                        "message": NO_TABLE.format(name=table),
                    }
                )
        hidden = stored.get(rid)
        if hidden is not None and not (
            hidden["from"]["table"] in allowed and hidden["to"]["table"] in allowed
        ):
            found.append({"path": f"/relationships/{rid}", "message": "Choose another id."})
    return found


def _conflict(base: int, now: int) -> ApiError:
    return ApiError(
        409,
        "version_conflict",
        f"The model has changed since version {base} (it is now version {now}) in the same "
        "place as your change. Load the latest version and apply your change again.",
        [{"path": "/baseVersion", "message": f"Current version is {now}."}],
    )


def plan(
    db: Session,
    settings: Settings,
    state: State,
    allowed: Visible,
    *,
    document: Any | None = None,
    ops: Sequence[Mapping[str, Any]] | None = None,
    base_version: int | None = None,
) -> tuple[dict[str, Any], Any | None]:
    """The whole document a change makes (the parts the person may not use put back
    as they are), and the layout it carries, if any. A stale change is rebased or
    refused (409); one with a broken structure is refused (422)."""
    if (document is None) == (ops is None):
        raise ApiError(422, "invalid_request", "Send either a document or JSON Patch `ops`.")
    layout = None
    if document is not None:
        found = schema_check(settings, document)
        if found:
            raise _invalid(found)
        layout = document.get("layout")
    visible = ws.visible_part(state.effective, allowed)
    if base_version is not None and base_version != state.version:
        if not 0 <= base_version < state.version:
            raise ApiError(
                409,
                "version_conflict",
                f"There is no version {base_version} to start from: the model is at version "
                f"{state.version}.",
                [{"path": "/baseVersion", "message": f"Current version is {state.version}."}],
            )
        base, _ = ws.effective(version_document(db, base_version), state.datasets)
        if document is not None:
            # The change, as what it did to the version it started from.
            ops = ws.diff_ops(ws.visible_part(base, allowed), ws.definition_of(document))
            document = None
        theirs = ws.changed_paths(base, state.effective)
        if ops and ws.overlaps(ws.touched_paths(ops), theirs):
            raise _conflict(base_version, state.version)
    if document is not None:
        edited = document
    else:
        edited = patches.apply(visible, list(ops or [])) if ops else visible
        if isinstance(edited, dict) and "layout" in edited:
            layout = edited["layout"]
    found = schema_check(settings, edited)
    if found:
        raise _invalid(found)
    edited = ws.definition_of(edited)
    found = _names_hidden(edited, state, allowed)
    if found:
        raise _invalid(found)
    return ws.merge_visible(state.effective, edited, allowed), layout


def check(
    settings: Settings, state: State, allowed: Visible, full: Mapping[str, Any]
) -> tuple[list[semantic.Problem], list[semantic.Problem]]:
    """The problems a change would add, which stop a save, and the ones the model
    already had, each as the person may read them."""
    stored = ws.normalise(full, state.stored, state.datasets)
    found = schema_check(settings, stored)
    if found:
        return _redact(found, allowed, full), []
    before = problems_of(settings, state.stored, state, allowed)
    after = problems_of(settings, stored, state, allowed)
    new = [p for p in after if p not in before]
    old = [p for p in after if p in before and not _hidden(p["path"], allowed, full)]
    return _redact(new, allowed, full), old


def validate(
    db: Session,
    settings: Settings,
    principal: Principal,
    *,
    document: Any | None = None,
    ops: Sequence[Mapping[str, Any]] | None = None,
    base_version: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """What a save would say, without saving: the problems that would stop it, the
    ones the model already has, the warnings it would be saved with (those that need
    no data), and the part of the document it would make that the person may use.
    Also the whole document, when it can be saved (for `services.model_warnings`)."""
    state = current(db)
    allowed = visible_to(db, principal)
    try:
        full, _ = plan(
            db, settings, state, allowed, document=document, ops=ops, base_version=base_version
        )
    except ApiError as exc:
        if exc.status != 422 or exc.code != "invalid_model":
            raise
        checked = {"valid": False, "problems": exc.details, "existing": [], "warnings": []}
        return checked, None
    new, old = check(settings, state, allowed, full)
    stored = ws.normalise(full, state.stored, state.datasets)
    warnings = [] if new else ws.structure_warnings(stored, state.datasets)
    checked = {
        "valid": not new,
        "problems": new,
        "existing": old,
        "warnings": [w for w in warnings if not warning_hidden(w, allowed, full)],
        "document": ws.visible_part(full, allowed),
    }
    return checked, None if new else full


def _changed_names(old: Mapping[str, Any], new: Mapping[str, Any], key: str) -> list[str]:
    paths = ws.changed_paths(old.get(key) or {}, new.get(key) or {})
    return sorted({p.split("/")[1].replace("~1", "/").replace("~0", "~") for p in paths})


def _audit_detail(old: Mapping[str, Any], new: Mapping[str, Any], version: int) -> dict[str, Any]:
    return {
        "version": version,
        "tables": _changed_names(old, new, "tables")[:50],
        "relationships": _changed_names(old, new, "relationships")[:50],
        "rls_rules": sum(len(t.get("rls") or []) for t in (new.get("tables") or {}).values()),
    }


def _set_layout(record: WorkspaceModel, state: State, allowed: Visible, layout: Any) -> None:
    tables = layout.get("tables") or {}
    kept = {n: p for n, p in (state.layout.get("tables") or {}).items() if n not in allowed}
    mine = {n: p for n, p in tables.items() if n in allowed and n in state.effective["tables"]}
    record.layout = {
        **{k: v for k, v in layout.items() if k != "tables"},
        "tables": {**kept, **mine},
    }
    flag_modified(record, "layout")


def save(
    db: Session,
    settings: Settings,
    actor: Principal,
    *,
    document: Any | None = None,
    ops: Sequence[Mapping[str, Any]] | None = None,
    base_version: int | None = None,
    message: str | None = None,
    author_type: str = "user",
) -> State:
    """Store a change as a new version. Nothing is written when it changes nothing;
    a layout it carries is saved without a version."""
    record = _record(db, lock=True)
    assert record is not None
    state = _state(record, _rows(db))
    allowed = visible_to(db, actor)
    try:
        full, layout = plan(
            db, settings, state, allowed, document=document, ops=ops, base_version=base_version
        )
        stored = ws.normalise(full, state.stored, state.datasets)
        changed = not _same(stored, state.stored)
        if changed:
            new, _ = check(settings, state, allowed, full)
            if new:
                raise _invalid(new)
    except ApiError:
        db.rollback()
        raise
    if isinstance(layout, dict):
        _set_layout(record, state, allowed, layout)
    if changed:
        record.definition = stored
        flag_modified(record, "definition")  # a pure reordering compares equal otherwise
        record.version = state.version + 1
        record.updated_by = actor.user_id
        db.add(
            WorkspaceModelVersion(
                version=record.version,
                definition=stored,
                author_type=author_type,
                author_id=actor.user_id,
                message=(message or "").strip()[:500] or None,
            )
        )
        audit.record(
            db,
            action="model.updated",
            actor=actor,
            actor_type=author_type,
            target_type="workspace_model",
            target_id="workspace",
            detail=_audit_detail(state.stored, stored, record.version),
        )
    db.commit()
    return current(db)


def save_layout(db: Session, settings: Settings, actor: Principal, layout: Any) -> dict[str, Any]:
    """Where the Model view draws the tables. Not versioned."""
    found = schema_check(settings, {**ws.empty_document(), "layout": layout})
    if found:
        raise ApiError(422, "invalid_layout", "The layout is not valid.", found)
    record = _record(db, lock=True)
    assert record is not None
    state = _state(record, _rows(db))
    allowed = visible_to(db, actor)
    _set_layout(record, state, allowed, layout)
    db.commit()
    return visible_layout(current(db), allowed)


def remove_tables(db: Session, names: Iterable[str], message: str) -> None:
    """Take tables and their relationships out of the model, as a new version by the
    system (a dataset was deleted). Does not commit."""
    gone = set(names)
    record = _record(db, lock=True)
    assert record is not None
    stored = ws.definition_of(record.definition)
    tables = {n: t for n, t in stored["tables"].items() if n not in gone}
    relationships = {
        rid: rel
        for rid, rel in stored["relationships"].items()
        if rel["from"]["table"] not in gone and rel["to"]["table"] not in gone
    }
    layout = dict(record.layout or {})
    if any(name in gone for name in layout.get("tables") or {}):
        layout["tables"] = {n: p for n, p in layout["tables"].items() if n not in gone}
        record.layout = layout
        flag_modified(record, "layout")
    if tables == stored["tables"] and relationships == stored["relationships"]:
        return
    stored = {**stored, "tables": tables, "relationships": relationships}
    record.definition = stored
    flag_modified(record, "definition")
    record.version += 1
    db.add(
        WorkspaceModelVersion(
            version=record.version, definition=stored, author_type="system", message=message
        )
    )


# ---- history ------------------------------------------------------------------------


def version_entry(row: WorkspaceModelVersion, author: str | None) -> dict[str, Any]:
    return {
        "version": row.version,
        "authorType": row.author_type,
        "authorName": author
        or {"system": "System", "agent": "Assistant"}.get(row.author_type, "Deleted user"),
        "message": row.message,
        "createdAt": row.created_at.isoformat(),
        "current": False,
    }


def as_seen(document: Mapping[str, Any], state: State, allowed: Visible) -> dict[str, Any]:
    """A version as the person may read it, with its starter tables."""
    return ws.visible_part(ws.effective(document, state.datasets)[0], allowed)


def list_versions(db: Session, principal: Principal) -> list[dict[str, Any]]:
    """The versions, newest first. Someone who may not use every table sees the
    versions that changed the part they may use."""
    state = current(db)
    allowed = visible_to(db, principal)
    rows = db.execute(
        select(WorkspaceModelVersion, User.display_name)
        .outerjoin(User, User.id == WorkspaceModelVersion.author_id)
        .order_by(WorkspaceModelVersion.version.desc())
        .limit(MAX_VERSIONS_LISTED + 1)
    ).all()
    listed = []
    for index, (row, name) in enumerate(rows[:MAX_VERSIONS_LISTED]):
        if allowed.blocked:
            if index + 1 < len(rows):
                before: dict[str, Any] | None = as_seen(
                    rows[index + 1][0].definition, state, allowed
                )
            elif row.version == 1:
                before = as_seen(ws.empty_document(), state, allowed)
            else:
                before = None  # older than the list goes: show it
            if before is not None and _same(before, as_seen(row.definition, state, allowed)):
                continue
        listed.append(version_entry(row, name))
    if listed:
        listed[0]["current"] = True
    return listed


def get_version(db: Session, principal: Principal, number: int) -> dict[str, Any]:
    row = db.execute(
        select(WorkspaceModelVersion, User.display_name)
        .outerjoin(User, User.id == WorkspaceModelVersion.author_id)
        .where(WorkspaceModelVersion.version == number)
    ).first()
    if row is None:
        raise ApiError(404, "not_found", f"There is no version {number} of the model.")
    version, name = row
    state = current(db)
    document = as_seen(version.definition, state, visible_to(db, principal))
    return {
        **version_entry(version, name),
        "current": version.version == state.version,
        "document": document,
        "yaml": to_yaml(document),
    }


def diff(db: Session, principal: Principal, first: int, second: int) -> dict[str, Any]:
    """What changed from one version to another, in the part the person may use."""
    state = current(db)
    allowed = visible_to(db, principal)
    old = as_seen(version_document(db, first), state, allowed)
    new = as_seen(version_document(db, second), state, allowed)
    return {
        "from": first,
        "to": second,
        "tables": _changed_names(old, new, "tables"),
        "relationships": _changed_names(old, new, "relationships"),
        "ops": ws.diff_ops(old, new),
    }


def restore(db: Session, settings: Settings, actor: Principal, number: int) -> State:
    """Make the part of an old version the person may use current again, as a new
    version. History is never rewritten. Tables whose datasets are gone stay out."""
    state = current(db)
    old = as_seen(version_document(db, number), state, visible_to(db, actor))
    gone = sorted(name for name in old["tables"] if name not in state.datasets)
    old["tables"] = {n: t for n, t in old["tables"].items() if n not in gone}
    old["relationships"] = {
        rid: rel
        for rid, rel in old["relationships"].items()
        if rel["from"]["table"] not in gone and rel["to"]["table"] not in gone
    }
    message = f"Restored version {number}"
    if gone:
        message += f" (without {', '.join(gone)}: they have no data now)"
    db.rollback()  # `save` locks the model and reads it again
    return save(db, settings, actor, document=old, message=message)
