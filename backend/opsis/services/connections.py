"""Connections: validation, encrypted secrets, test and discovery."""

from __future__ import annotations

import copy
import shutil
import uuid
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from opsis.auth.principal import Principal
from opsis.connectors.base import ConnectorError, ReconnectNeeded, StreamSpec
from opsis.connectors.convert import INVALID_DATES
from opsis.connectors.registry import get_registry
from opsis.db.models import Connection, Dataset
from opsis.errors import ApiError
from opsis.services import audit, crypto
from opsis.settings import Settings


def uploads_dir(settings: Settings) -> Any:
    return settings.data_dir / "uploads"


def connector_class(settings: Settings, connector_id: str) -> Any:
    cls = get_registry(settings.plugins_dir).get(connector_id)
    if cls is None:
        raise ApiError(404, "unknown_connector", f"There is no connector called {connector_id!r}.")
    return cls


# A setting every connection has: what to do with dates that cannot exist, which
# legacy databases hold (MySQL's 0000-00-00 in a non-strict sql_mode). See
# opsis.connectors.convert.
INVALID_DATES_FIELD: dict[str, Any] = {
    "type": "string",
    "title": "Invalid dates",
    "description": "Dates that cannot exist, such as 0000-00-00 or 2020-05-00, which old "
    "databases can hold. Each refresh reports how many it found, column by column.",
    "enum": list(INVALID_DATES),
    "x-enum-labels": {
        "null": "Store them as empty (NULL)",
        "text": "Read date columns as text, exactly as stored",
        "fail": "Stop the refresh",
    },
    "default": "null",
}


def form_schema(cls: Any) -> dict[str, Any]:
    """A connector's settings plus the ones every connection has."""
    schema: dict[str, Any] = copy.deepcopy(cls.config_schema())
    schema.setdefault("properties", {})["invalid_dates"] = INVALID_DATES_FIELD
    return schema


def invalid_dates(connection: Connection) -> str:
    chosen = str((connection.config or {}).get("invalid_dates") or "null")
    return chosen if chosen in INVALID_DATES else "null"


def list_connectors(settings: Settings) -> list[dict[str, Any]]:
    return [
        {"id": c.id, "name": c.name, "version": c.version, "configSchema": form_schema(c)}
        for c in get_registry(settings.plugins_dir).all()
    ]


def _fields(schema: dict[str, Any], marker: str) -> set[str]:
    return {k for k, v in schema.get("properties", {}).items() if v.get(marker)}


def _validate(schema: dict[str, Any], config: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    if errors:
        raise ApiError(
            422,
            "invalid_config",
            "The connection settings are not valid.",
            [
                {
                    "path": "/config/" + "/".join(str(p) for p in e.absolute_path),
                    "message": e.message,
                }
                for e in errors
            ],
        )


def _split(
    settings: Settings,
    connector_id: str,
    submitted: dict[str, Any],
    existing_secrets: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate submitted settings and separate public values from secrets.

    A secret field left empty keeps its saved value, so editing a connection
    never requires typing the password again.
    """
    schema = form_schema(connector_class(settings, connector_id))
    secret_names = _fields(schema, "x-secret")
    cleaned = {k: v for k, v in submitted.items() if v is not None and v != ""}
    secrets = dict(existing_secrets or {})
    secrets.update({k: v for k, v in cleaned.items() if k in secret_names})
    public = {k: v for k, v in cleaned.items() if k not in secret_names}
    # Keys starting with __ are kept by the app itself (a saved OAuth sign-in), not the form.
    _validate(schema, {**public, **{k: v for k, v in secrets.items() if not k.startswith("__")}})
    return public, secrets


def instantiate(settings: Settings, connection: Connection) -> Any:
    try:
        secrets = crypto.decrypt(settings, connection.secrets)
    except crypto.SecretsError as exc:
        raise ConnectorError(str(exc)) from exc
    return _build(settings, connection.connector_id, connection.config, secrets)


def _build(
    settings: Settings, connector_id: str, public: dict[str, Any], secrets: dict[str, Any]
) -> Any:
    cls = connector_class(settings, connector_id)
    internal: dict[str, Any] = {
        "_uploads_dir": str(uploads_dir(settings)),
        "_calendar": {
            "weekStart": settings.week_start,
            "fiscalYearStartMonth": settings.fiscal_year_start_month,
            "fiscalYearLabel": settings.fiscal_year_label,
            "timezone": settings.workspace_timezone,
        },
    }
    if settings.google_api_base_url:
        internal["_google_api_base"] = settings.google_api_base_url  # tests only
    return cls({**public, **secrets, **internal})


def serialize(settings: Settings, connection: Connection, dataset_count: int = 0) -> dict[str, Any]:
    cls = get_registry(settings.plugins_dir).get(connection.connector_id)
    secret_names = _fields(cls.config_schema(), "x-secret") if cls else set()
    try:
        saved = set(crypto.decrypt(settings, connection.secrets))
        readable = True
    except crypto.SecretsError:
        saved, readable = set(), False
    return {
        "id": str(connection.id),
        "name": connection.name,
        "connectorId": connection.connector_id,
        "connectorName": cls.name if cls else connection.connector_id,
        "config": connection.config,  # never contains secret fields
        "secretFields": sorted(saved & secret_names),
        "secretsReadable": readable,
        "lastTest": None
        if connection.last_tested_at is None
        else {
            "ok": connection.last_test_ok,
            "message": connection.last_test_message,
            "at": connection.last_tested_at.isoformat(),
        },
        "datasetCount": dataset_count,
        "createdAt": connection.created_at.isoformat(),
        # A saved sign-in (Google Sheets OAuth): account, scopes, status. Never the token.
        "auth": connection.auth_state,
    }


def list_connections(db: Session, settings: Settings) -> list[dict[str, Any]]:
    counts = dict(
        db.execute(
            select(Dataset.connection_id, func.count()).group_by(Dataset.connection_id)
        ).all()
    )
    rows = db.scalars(select(Connection).order_by(Connection.name)).all()
    return [serialize(settings, c, counts.get(c.id, 0)) for c in rows]


def get(db: Session, connection_id: uuid.UUID) -> Connection:
    connection = db.get(Connection, connection_id)
    if connection is None:
        raise ApiError(404, "not_found", "Connection not found.")
    return connection


def _check_name(db: Session, name: str, ignore: uuid.UUID | None = None) -> str:
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_request", "Give the connection a name.")
    clash = db.scalar(select(Connection).where(func.lower(Connection.name) == name.lower()))
    if clash is not None and clash.id != ignore:
        raise ApiError(409, "name_taken", f"A connection called {name!r} already exists.")
    return name


def create(
    db: Session,
    settings: Settings,
    actor: Principal,
    *,
    name: str,
    connector_id: str,
    config: dict[str, Any],
) -> Connection:
    public, secrets = _split(settings, connector_id, config)
    connection = Connection(
        name=_check_name(db, name),
        connector_id=connector_id,
        config=public,
        secrets=crypto.encrypt(settings, secrets),
        created_by=actor.user_id,
    )
    db.add(connection)
    db.flush()
    audit.record(
        db,
        action="connection.created",
        actor=actor,
        target_type="connection",
        target_id=str(connection.id),
        detail={"name": connection.name, "connector": connector_id},
    )
    db.commit()
    return connection


def update(
    db: Session,
    settings: Settings,
    actor: Principal,
    connection: Connection,
    *,
    name: str | None,
    config: dict[str, Any] | None,
) -> Connection:
    if name is not None:
        connection.name = _check_name(db, name, ignore=connection.id)
    if config is not None:
        try:
            existing = crypto.decrypt(settings, connection.secrets)
        except crypto.SecretsError:
            existing = {}  # unreadable: the user must enter them again
        public, secrets = _split(settings, connection.connector_id, config, existing)
        connection.config = public
        from opsis.services import google_oauth

        google_oauth.forget_if_client_changed(connection, existing, secrets)
        connection.secrets = crypto.encrypt(settings, secrets)
        connection.last_test_ok = connection.last_test_message = connection.last_tested_at = None
    audit.record(
        db,
        action="connection.updated",
        actor=actor,
        target_type="connection",
        target_id=str(connection.id),
        detail={"name": connection.name, "config_changed": config is not None},
    )
    db.commit()
    return connection


def delete(db: Session, settings: Settings, actor: Principal, connection: Connection) -> None:
    in_use = db.scalar(
        select(func.count()).select_from(Dataset).where(Dataset.connection_id == connection.id)
    )
    if in_use:
        raise ApiError(
            409,
            "in_use",
            f"{in_use} dataset(s) use this connection. Delete them first.",
        )
    upload = str(connection.config.get("file", "")).split("/")[0]
    audit.record(
        db,
        action="connection.deleted",
        actor=actor,
        target_type="connection",
        target_id=str(connection.id),
        detail={"name": connection.name},
    )
    db.delete(connection)
    db.commit()
    if upload:
        folder = (uploads_dir(settings) / upload).resolve()
        if folder.parent == uploads_dir(settings).resolve():
            shutil.rmtree(folder, ignore_errors=True)


def test_unsaved(
    db: Session,
    settings: Settings,
    connector_id: str,
    config: dict[str, Any],
    connection_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Test settings from the form. With connection_id, blank secrets use the saved ones."""
    existing: dict[str, Any] = {}
    if connection_id is not None:
        existing = crypto.decrypt(settings, get(db, connection_id).secrets)
    public, secrets = _split(settings, connector_id, config, existing)
    connector = _build(settings, connector_id, public, secrets)
    try:
        result = connector.test_connection()
    except ReconnectNeeded as exc:
        if connection_id is not None:
            from opsis.services import google_oauth

            google_oauth.mark_reconnect(db, settings, get(db, connection_id), str(exc))
        return {"ok": False, "message": str(exc)}
    finally:
        connector.close()
    return {"ok": result.ok, "message": result.message}


def test_saved(db: Session, settings: Settings, connection: Connection) -> dict[str, Any]:
    try:
        connector = instantiate(settings, connection)
        try:
            result = connector.test_connection()
            ok, message = result.ok, result.message
        finally:
            connector.close()
    except ReconnectNeeded as exc:
        from opsis.services import google_oauth

        google_oauth.mark_reconnect(db, settings, connection, str(exc))
        ok, message = False, str(exc)
    except ConnectorError as exc:
        ok, message = False, str(exc)
    connection.last_test_ok = ok
    connection.last_test_message = message
    connection.last_tested_at = datetime.now(UTC)
    db.commit()
    return {"ok": ok, "message": message}


def stream_dict(spec: StreamSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "label": spec.label or spec.name,
        "columns": [
            {"name": c.name, "type": c.type.value, "sourceType": c.source_type}
            for c in spec.columns
        ],
        "primaryKey": list(spec.primary_key),
        "cursorCandidates": list(spec.cursor_candidates),
        "supportedModes": [m.value for m in spec.supported_modes],
        "rowCountEstimate": spec.row_count_estimate,
    }


def discover(settings: Settings, connection: Connection) -> list[StreamSpec]:
    try:
        connector = instantiate(settings, connection)
        try:
            streams: list[StreamSpec] = connector.discover()
        finally:
            connector.close()
    except ReconnectNeeded as exc:
        db = object_session(connection)
        if db is not None:
            from opsis.services import google_oauth

            google_oauth.mark_reconnect(db, settings, connection, str(exc))
        raise ApiError(409, "needs_reconnect", str(exc)) from exc
    except ConnectorError as exc:
        raise ApiError(400, "connector_error", str(exc)) from exc
    return streams
