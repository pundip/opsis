"""Semantic models: storage, validation, starter generation, YAML import and export."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from opsis.auth.principal import Principal
from opsis.db.models import Dataset, SemanticModel, SemanticModelVersion, Snapshot, User
from opsis.errors import ApiError
from opsis.semantic import model as semantic
from opsis.services import audit
from opsis.settings import Settings

MAX_YAML_BYTES = 500_000
_BOOL_TAG = "tag:yaml.org,2002:bool"
_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _strict_booleans(base: type[Any]) -> Any:
    """A YAML loader or dumper where only true and false are booleans.

    YAML 1.1 also reads on, off, yes and no as booleans, which would turn the
    `on:` key of a join into `True`. Models use the YAML 1.2 rule instead.
    """
    cls: Any = type(f"Opsis{base.__name__}", (base,), {})
    cls.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != _BOOL_TAG]
        for first, resolvers in base.yaml_implicit_resolvers.items()
    }
    cls.add_implicit_resolver(_BOOL_TAG, _BOOL_RE, list("tTfF"))
    return cls


_Loader = _strict_booleans(yaml.SafeLoader)
_Dumper = _strict_booleans(yaml.SafeDumper)


def _columns(snapshot: Snapshot | None) -> dict[str, str]:
    return {c["name"]: c["type"] for c in snapshot.columns} if snapshot else {}


def dataset_by_name(db: Session, name: str) -> Dataset:
    dataset = db.scalar(select(Dataset).where(Dataset.name == name))
    if dataset is None:
        raise ApiError(404, "not_found", f"There is no dataset called {name!r}.")
    return dataset


def current_columns(db: Session, dataset: Dataset) -> dict[str, str]:
    snapshot = (
        db.get(Snapshot, dataset.current_snapshot_id) if dataset.current_snapshot_id else None
    )
    return _columns(snapshot)


def all_columns(db: Session) -> dict[str, dict[str, str]]:
    """Column names and types of every dataset that has data, by dataset name."""
    rows = db.execute(
        select(Dataset.name, Snapshot).join(Snapshot, Snapshot.id == Dataset.current_snapshot_id)
    ).all()
    return {name: _columns(snapshot) for name, snapshot in rows}


def starter(dataset: Dataset, columns: dict[str, str]) -> dict[str, Any]:
    return semantic.starter_definition(dataset.label, columns, tuple(dataset.primary_key))


def ensure_starter(db: Session, dataset: Dataset, columns: dict[str, str]) -> None:
    """Give a dataset its first model, or refresh a starter nobody has edited.

    Called by the refresh job inside its transaction. Does not commit.
    """
    existing = db.scalar(select(SemanticModel).where(SemanticModel.dataset_id == dataset.id))
    generated = starter(dataset, columns)
    if existing is None:
        record = SemanticModel(dataset_id=dataset.id, definition=generated, version=0)
        db.add(record)
        _write_version(db, record, generated, "system", None, "Generated from the columns")
    elif existing.is_starter and _dumps(existing.definition) != _dumps(generated):
        _write_version(db, existing, generated, "system", None, "Regenerated: the columns changed")


def _dumps(definition: dict[str, Any]) -> str:
    return json.dumps(definition, sort_keys=False)


def _write_version(
    db: Session,
    record: SemanticModel,
    definition: dict[str, Any],
    author_type: str,
    author_id: uuid.UUID | None,
    message: str | None,
) -> None:
    """Make `definition` the current model and add it to the history. Does not commit."""
    record.version = (record.version or 0) + 1
    record.definition = definition
    # Dicts compare equal whatever their key order, so a pure reordering
    # would otherwise not be written.
    flag_modified(record, "definition")
    db.add(
        SemanticModelVersion(
            dataset_id=record.dataset_id,
            version=record.version,
            definition=definition,
            author_type=author_type,
            author_id=author_id,
            message=(message or "").strip()[:500] or None,
        )
    )


def get_record(db: Session, dataset: Dataset) -> SemanticModel | None:
    return db.scalar(select(SemanticModel).where(SemanticModel.dataset_id == dataset.id))


def to_yaml(dataset_name: str, definition: dict[str, Any]) -> str:
    document = {"dataset": dataset_name, **{k: v for k, v in definition.items() if k != "dataset"}}
    text: str = yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    return text


def from_yaml(text: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_YAML_BYTES:
        raise ApiError(413, "too_large", "The YAML document is too large.")
    try:
        document = yaml.load(text, Loader=_Loader)  # noqa: S506  a SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or "it could not be parsed"
        raise ApiError(422, "invalid_yaml", f"The YAML is not valid: {problem}{where}.") from None
    if not isinstance(document, dict):
        raise ApiError(
            422, "invalid_yaml", "The YAML must be a mapping with keys such as dimensions."
        )
    return document


def _fields(dataset_name: str, model: semantic.Model, joined: bool) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = [
        {
            "ref": f"{dataset_name}.{d.name}",
            "kind": "dimension",
            "type": d.type,
            "label": d.label,
            "grains": list(d.grains),
            "dataset": dataset_name,
        }
        for d in model.dimensions.values()
    ]
    if not joined:  # measures and segments of joined datasets cannot be queried from here
        fields += [
            {
                "ref": f"{dataset_name}.{m.name}",
                "kind": "measure",
                "type": "number",
                "label": m.label,
                "format": m.format,
                "dataset": dataset_name,
            }
            for m in model.measures.values()
        ]
        fields += [
            {
                "ref": f"{dataset_name}.{s.name}",
                "kind": "segment",
                "type": "boolean",
                "label": s.label,
                "dataset": dataset_name,
            }
            for s in model.segments.values()
        ]
    return fields


def describe(db: Session, dataset: Dataset) -> dict[str, Any]:
    """Everything the model editor and the query builder need for one dataset."""
    columns = current_columns(db, dataset)
    record = get_record(db, dataset)
    definition = record.definition if record else starter(dataset, columns) if columns else {}
    model = semantic.parse_model(dataset.name, definition)
    fields = _fields(dataset.name, model, joined=False)
    for join in model.joins:
        if join.relationship not in semantic.SAFE_JOINS:
            continue
        other = db.scalar(select(Dataset).where(Dataset.name == join.to))
        other_record = get_record(db, other) if other else None
        if other_record:
            fields += _fields(join.to, semantic.parse_model(join.to, other_record.definition), True)
    return {
        "dataset": {
            "id": str(dataset.id),
            "name": dataset.name,
            "label": dataset.label,
            "hasData": bool(columns),
            "columns": [{"name": n, "type": t} for n, t in columns.items()],
        },
        "definition": definition,
        "yaml": to_yaml(dataset.name, definition),
        "fields": fields,
        "isStarter": record.is_starter if record else True,
        "saved": record is not None,
        "version": record.version if record else 0,
        "updatedAt": record.updated_at.isoformat() if record else None,
    }


def list_models(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Dataset, SemanticModel, Snapshot)
        .outerjoin(SemanticModel, SemanticModel.dataset_id == Dataset.id)
        .outerjoin(Snapshot, Snapshot.id == Dataset.current_snapshot_id)
        .order_by(Dataset.name)
    ).all()
    result = []
    for dataset, record, snapshot in rows:
        # Datasets refreshed before models existed have none stored: show the starter.
        definition = (
            record.definition
            if record
            else starter(dataset, _columns(snapshot))
            if snapshot
            else {}
        )
        result.append(
            {
                "dataset": dataset.name,
                "label": dataset.label,
                "hasData": dataset.current_snapshot_id is not None,
                "saved": record is not None,
                "isStarter": record.is_starter if record else True,
                "dimensions": len(definition.get("dimensions") or {}),
                "measures": len(definition.get("measures") or {}),
                "joins": [j["to"] for j in definition.get("joins") or []],
                "rlsRules": len(definition.get("rls") or []),
                "updatedAt": record.updated_at.isoformat() if record else None,
            }
        )
    return result


def problems(
    db: Session, settings: Settings, dataset: Dataset, definition: Any
) -> list[semantic.Problem]:
    columns = current_columns(db, dataset)
    if not columns:
        return [
            {
                "path": "/",
                "message": "This dataset has no data yet. Refresh it before defining a model.",
            }
        ]
    others = {name: cols for name, cols in all_columns(db).items() if name != dataset.name}
    return semantic.validate(settings.spec_dir, dataset.name, definition, columns, others)


def resolve_input(definition: Any | None, yaml_text: str | None) -> Any:
    if (definition is None) == (yaml_text is None):
        raise ApiError(422, "invalid_request", "Send either `definition` or `yaml`.")
    return from_yaml(yaml_text) if yaml_text is not None else definition


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
) -> SemanticModel:
    """Validate and store a model as a new version.

    `base_version` is the version the author started from. If someone else has
    saved since, the save is refused with 409 so nothing is silently overwritten
    (the agent re-reads and retries; a person sees the message).
    """
    record = db.scalar(
        select(SemanticModel).where(SemanticModel.dataset_id == dataset.id).with_for_update()
    )
    current = record.version if record else 0
    if base_version is not None and base_version != current:
        raise ApiError(
            409,
            "version_conflict",
            f"The model has changed since version {base_version} (it is now version {current}). "
            "Load the latest version and apply your change again.",
            [{"path": "/baseVersion", "message": f"Current version is {current}."}],
        )
    found = problems(db, settings, dataset, definition)
    if found:
        raise ApiError(422, "invalid_model", "The model is not valid.", found)
    stored = {k: v for k, v in definition.items() if k != "dataset"}
    if record is None:
        # Continue after any history that already exists for this dataset.
        latest = db.scalar(
            select(func.max(SemanticModelVersion.version)).where(
                SemanticModelVersion.dataset_id == dataset.id
            )
        )
        record = SemanticModel(dataset_id=dataset.id, definition=stored, version=latest or 0)
        db.add(record)
    _write_version(db, record, stored, author_type, actor.user_id, message)
    record.is_starter = False
    record.updated_by = actor.user_id
    audit.record(
        db,
        action="model.updated",
        actor=actor,
        actor_type=author_type,
        target_type="dataset",
        target_id=str(dataset.id),
        detail={
            "dataset": dataset.name,
            "version": record.version,
            "rls_rules": len(stored.get("rls") or []),
        },
    )
    db.commit()
    return record


def _version_dict(
    version: SemanticModelVersion, author: str | None, current: int
) -> dict[str, Any]:
    return {
        "version": version.version,
        "authorType": version.author_type,
        "authorName": author
        or {"system": "System", "agent": "Assistant"}.get(version.author_type, "Deleted user"),
        "message": version.message,
        "createdAt": version.created_at.isoformat(),
        "current": version.version == current,
    }


def list_versions(db: Session, dataset: Dataset) -> list[dict[str, Any]]:
    record = get_record(db, dataset)
    rows = db.execute(
        select(SemanticModelVersion, User.display_name)
        .outerjoin(User, User.id == SemanticModelVersion.author_id)
        .where(SemanticModelVersion.dataset_id == dataset.id)
        .order_by(SemanticModelVersion.version.desc())
        .limit(200)
    ).all()
    current = record.version if record else 0
    return [_version_dict(version, name, current) for version, name in rows]


def get_version(db: Session, dataset: Dataset, number: int) -> dict[str, Any]:
    row = db.execute(
        select(SemanticModelVersion, User.display_name)
        .outerjoin(User, User.id == SemanticModelVersion.author_id)
        .where(
            SemanticModelVersion.dataset_id == dataset.id, SemanticModelVersion.version == number
        )
    ).first()
    if row is None:
        raise ApiError(404, "not_found", f"There is no version {number} of this model.")
    version, name = row
    record = get_record(db, dataset)
    return {
        **_version_dict(version, name, record.version if record else 0),
        "definition": version.definition,
        "yaml": to_yaml(dataset.name, version.definition),
    }


def restore(
    db: Session, settings: Settings, actor: Principal, dataset: Dataset, number: int
) -> SemanticModel:
    """Make an old version current again, as a new version. History is never rewritten."""
    old = get_version(db, dataset, number)
    return save(
        db, settings, actor, dataset, old["definition"], message=f"Restored version {number}"
    )
