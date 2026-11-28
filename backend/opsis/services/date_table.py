"""Generate a date table (V2_DESIGN 1.3): a dataset from the built-in date connector
that runs from the start of the financial year of the earliest date in the model to
the end of the next financial year, refreshed every day, marked as the model's date
table. Relationships from date columns are offered, not made: people confirm them.

The connection it makes reads nothing outside Opsis and holds no secrets, so anyone
who may create datasets and edit the model may generate one (ADR-039).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlglot import exp

from opsis.auth.principal import Permission, Principal
from opsis.connectors.date import STREAM
from opsis.db.models import Connection, Dataset, Snapshot
from opsis.errors import ApiError
from opsis.semantic import workspace as ws
from opsis.semantic.calendar import period_start, shift_period
from opsis.services import connections, datasets, model_suggestions, schedules
from opsis.services import query as queries
from opsis.services import workspace_model as wm
from opsis.settings import Settings

CONNECTOR = "date"
DEFAULT_NAME = "dates"
DEFAULT_LABEL = "Dates"
DAILY = "5 0 * * *"  # just after midnight in the workspace timezone
EARLIEST = date(1900, 1, 1)
LABELS = {
    "date": "Date",
    "year": "Year",
    "quarter": "Quarter number",
    "quarter_label": "Quarter",
    "month": "Month number",
    "month_name": "Month",
    "month_short": "Month (short)",
    "year_month": "Year and month",
    "week_start": "Week starting",
    "day_of_month": "Day of the month",
    "day_of_week": "Day of the week",
    "day_name": "Day",
    "day_short": "Day (short)",
    "day_of_year": "Day of the year",
    "is_weekend": "Weekend",
    "fiscal_year": "Financial year",
    "fiscal_year_label": "Financial year name",
    "fiscal_quarter": "Financial quarter",
    "fiscal_month": "Financial month",
}


def table_entry(data: ws.TableData, label: str) -> dict[str, Any]:
    """The date table as the model writes it: a dimension per column with a readable
    label, a count of days, and the mark."""
    entry = ws.starter_table(data)
    for dimension in entry["dimensions"].values():
        if dimension["column"] in LABELS:
            dimension["label"] = LABELS[dimension["column"]]
    return {
        **entry,
        "label": label,
        "measures": {"days": {"agg": "count", "label": "Days"}},
        "dateTable": {"column": "date"},
    }


def earliest_date(
    db: Session, settings: Settings, runtime: queries.QueryRuntime, principal: Principal
) -> tuple[date | None, bool]:
    """The earliest date or timestamp in the tables the person may use, as they see
    them, and whether some rows were left out (rules hide them, or a table could not
    be read)."""
    state = wm.current(db)
    allowed = wm.visible_to(db, principal)
    workspace = wm.workspace_of(settings, state)
    user = queries.user_context(principal, None)
    db.rollback()  # nothing to hold open while the data is read
    earliest: date | None = None
    partial = False
    for name, source in sorted(workspace.sources.items()):
        columns = [c for c, kind in source.columns.items() if kind in ("date", "timestamp")]
        if name not in allowed or not columns:
            continue
        try:
            partial = partial or queries.rls_applies(workspace, source, user)
            scan = queries.secured_select(workspace, source, user)
            sql = (
                exp.select(*[exp.Min(this=exp.column(c, table="t", quoted=True)) for c in columns])
                .from_(scan)
                .sql(dialect="duckdb")
            )
            rows = queries.run_raw(settings, runtime, source, sql, 1)
        except ApiError:
            partial = True
            continue
        for value in rows[0] if rows else []:
            try:
                day = date.fromisoformat(str(value)[:10]) if value is not None else None
            except ValueError:
                continue
            if day is not None and (earliest is None or day < earliest):
                earliest = day
    return earliest, partial


def _connection_name(db: Session, label: str) -> str:
    base = name = f"{label} (date table)"
    number = 2
    while db.scalar(select(Connection.id).where(func.lower(Connection.name) == name.lower())):
        name, number = f"{base} {number}", number + 1
    return name


def generate(
    db: Session,
    settings: Settings,
    runtime: queries.QueryRuntime,
    actor: Principal,
    *,
    name: str | None = None,
    label: str | None = None,
    start: str | None = None,
) -> dict[str, Any]:
    if not actor.can(Permission.DATASETS_EDIT):
        raise ApiError(403, "forbidden", "Making a date table needs permission to add datasets.")
    name = (name or DEFAULT_NAME).strip()
    label = (label or "").strip() or DEFAULT_LABEL
    if not datasets.NAME_RE.match(name):
        raise ApiError(
            422,
            "invalid_request",
            "Name the date table with lower-case letters, digits and underscores, starting "
            "with a letter (for example dates).",
        )
    if db.scalar(select(Dataset.id).where(Dataset.name == name)) is not None:
        raise ApiError(
            409,
            "name_taken",
            f"A dataset called {name!r} already exists. Choose another name for the date table.",
        )
    calendar = settings.calendar
    partial = False
    if start:
        try:
            first = date.fromisoformat(start)
        except ValueError:
            raise ApiError(
                422, "invalid_request", "`start` must be a date such as 2020-07-01."
            ) from None
    else:
        earliest, partial = earliest_date(db, settings, runtime, actor)
        first = period_start(earliest or settings.today(), "fiscal_year", calendar)
        if earliest is None:  # no dates yet: last financial year and this one
            first = shift_period(first, "fiscal_year", -1)
    first = max(first, EARLIEST)

    connection = connections.create(
        db,
        settings,
        actor,
        name=_connection_name(db, label),
        connector_id=CONNECTOR,
        config={"start": first.isoformat()},
    )
    end = connections.instantiate(settings, connection).span()[1]
    dataset = datasets.create(
        db, settings, actor, name=name, label=label, connection_id=connection.id, stream=STREAM,
        refresh_mode="full", cursor_field=None, primary_key=[], columns=None,
    )  # fmt: skip
    schedules.set_refresh(db, settings, actor, dataset, DAILY)
    job = datasets.refresh_now(db, settings, actor, dataset)
    if job.error:
        raise ApiError(
            500,
            "refresh_failed",
            f"The date table {name!r} was made, but its first refresh failed: {job.error}",
        )
    db.refresh(dataset)
    snapshot = db.get(Snapshot, dataset.current_snapshot_id)

    state = wm.current(db)
    entry = table_entry(state.datasets[name], label)
    db.rollback()  # `save` locks the model and reads it again
    saved = wm.save(
        db,
        settings,
        actor,
        ops=[{"op": "add", "path": f"/tables/{name}", "value": entry}],
        base_version=state.version,
        message=f"Date table {name}",
    )
    offered = model_suggestions.suggest(db, settings, runtime, actor, tables=[name])
    note = (
        f"{name!r} runs from {first} to {end} and is refreshed every day, so it reaches the "
        "end of the next financial year. Its financial-year columns follow the workspace "
        "calendar. `proposals` are relationships from date columns: nothing is related "
        "until you confirm them."
    )
    if not settings.feature_scheduled_refresh:
        note += (
            " Scheduled refreshes are off on this server (FEATURE_SCHEDULED_REFRESH), so "
            "it is refreshed only when someone refreshes it."
        )
    if partial:
        note += (
            " The first date comes from the rows you may see. If others hold earlier dates, "
            f"ask an admin to change the first date of the connection {connection.name!r}."
        )
    return {
        "dataset": {"id": str(dataset.id), "name": name, "label": label},
        "connection": {"id": str(connection.id), "name": connection.name},
        "table": name,
        "start": first.isoformat(),
        "end": end.isoformat(),
        "rows": snapshot.row_count if snapshot is not None else None,
        "version": saved.version,
        "proposals": offered["proposals"],
        "note": note,
    }
