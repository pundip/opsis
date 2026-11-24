"""Emailed dashboards (FEATURE_EMAIL with sign-in): a person asks for a dashboard
to be emailed to them on a schedule.

Each subscription is personal. Its schedule ("snapshot:<dashboard id>:<user
id>") belongs to the person, and every email is built as that person: their
dashboard access, their row-level security, the filters at their defaults.
If they can no longer open the dashboard when an email is due, the
subscription is removed and nothing is sent.

The email shows each visual's numbers (a KPI's value, a short table for the
others) and links to the dashboard. Charts are not drawn as pictures: mail
clients show tables reliably, and the link is one click away.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal
from opsis.db.models import Dashboard, Job, Schedule, User
from opsis.errors import ApiError, JobError
from opsis.semantic.calendar import CalendarConfig
from opsis.services import access as acl
from opsis.services import audit, mail, schedules, users
from opsis.services.query import QueryRuntime
from opsis.settings import Settings

log = logging.getLogger("opsis.subscriptions")

ROWS_SHOWN = 10
TEXT = "#18181b"
MUTED = "#71717a"
LINE = "#e4e4e7"
BAR = "#2563eb"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def key(dashboard_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"snapshot:{dashboard_id}:{user_id}"


def _person(principal: Principal) -> uuid.UUID:
    if principal.user_id is None or principal.auth_method == "share_link":
        raise ApiError(403, "forbidden", "Only a signed-in person can have a dashboard emailed.")
    return principal.user_id


def _answer(settings: Settings, principal: Principal, row: Schedule | None) -> dict[str, Any]:
    return {
        "subscription": schedules.describe(settings, row),
        "email": principal.email,
        "presets": list(schedules.PRESETS),
        "timezone": settings.workspace_timezone,
    }


def get(
    db: Session, settings: Settings, principal: Principal, dashboard: Dashboard
) -> dict[str, Any]:
    acl.require_dashboard(db, dashboard, principal, "view")
    row = db.scalar(select(Schedule).where(Schedule.key == key(dashboard.id, _person(principal))))
    return _answer(settings, principal, row)


def put(
    db: Session,
    settings: Settings,
    principal: Principal,
    dashboard: Dashboard,
    cron: str | None,
) -> dict[str, Any]:
    """Start, change or (cron None) stop emailing a dashboard to the caller."""
    acl.require_dashboard(db, dashboard, principal, "view")
    user_id = _person(principal)
    checked = schedules.check_cron(cron) if cron is not None else None
    row = schedules.put_schedule(
        db,
        settings,
        key=key(dashboard.id, user_id),
        kind="snapshot",
        cron=checked,
        target_id=dashboard.id,
        owner_id=user_id,
        created_by=user_id,
    )
    audit.record(
        db,
        action="dashboard.subscription_changed",
        actor=principal,
        target_type="dashboard",
        target_id=str(dashboard.id),
        detail={"title": dashboard.title, "cron": checked},
    )
    db.commit()
    return _answer(settings, principal, row)


def mine(db: Session, settings: Settings, principal: Principal) -> list[dict[str, Any]]:
    """The caller's emailed dashboards, soonest first."""
    rows = db.execute(
        select(Schedule, Dashboard)
        .join(Dashboard, Dashboard.id == Schedule.target_id)
        .where(Schedule.kind == "snapshot", Schedule.owner_id == _person(principal))
        .order_by(Schedule.next_run_at)
    ).all()
    found = []
    for row, dashboard in rows:
        found.append(
            {
                "dashboardId": str(dashboard.id),
                "title": dashboard.title,
                **(schedules.describe(settings, row) or {}),
            }
        )
    return found


# ---- the job ------------------------------------------------------------------------

_RUNTIME: QueryRuntime | None = None


def use_runtime(runtime: QueryRuntime) -> None:
    """With the inline runner, emails share the api's query runtime and its cache."""
    global _RUNTIME
    _RUNTIME = runtime


def _runtime(settings: Settings) -> QueryRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        from opsis.query.duckdb_engine import DuckDBEngine, ResultCache

        _RUNTIME = QueryRuntime(
            engine=DuckDBEngine(
                memory_limit_mb=settings.query_memory_limit_mb,
                threads=settings.query_threads,
                temp_dir=settings.data_dir / "tmp" / "duckdb",
            ),
            cache=ResultCache(settings.query_cache_entries),
            max_concurrent=1,
        )
    return _RUNTIME


def _stop(db: Session, row: Schedule, user: User | None, reason: str) -> dict[str, Any]:
    log.info("Emailed dashboard %s stopped: %s", row.key, reason)
    audit.record(
        db,
        action="dashboard.subscription_removed",
        actor=users.to_principal(user, "schedule") if user else None,
        target_type="dashboard",
        target_id=str(row.target_id),
        detail={"reason": reason},
    )
    db.delete(row)
    return {"sent": False, "removed": reason}


def run_snapshot(db: Session, job: Job, settings: Settings) -> dict[str, Any]:
    """Build one emailed dashboard as its person sees it, and send it."""
    row = db.get(Schedule, uuid.UUID(str(job.payload.get("schedule_id"))))
    if row is None or row.kind != "snapshot" or row.owner_id is None:
        return {"sent": False, "removed": "the subscription no longer exists"}
    user = db.get(User, row.owner_id)
    dashboard = db.get(Dashboard, row.target_id) if row.target_id else None
    if user is None or not user.is_active or user.is_builtin:
        return _stop(db, row, user, "the account is no longer active")
    if dashboard is None:
        return _stop(db, row, user, "the dashboard was deleted")
    principal = users.principal_for(db, user, "schedule")
    from opsis.services import dashboards

    try:
        acl.require_dashboard(db, dashboard, principal, "view")
        spec = dashboards.spec_for(db, dashboard, principal, "draft")
    except ApiError:
        return _stop(db, row, user, "the person can no longer open the dashboard")

    viewer = dashboards.viewer_context(db, principal, None)
    runtime = _runtime(settings)
    results: dict[str, dict[str, Any] | str] = {}
    for item in _reading_order(spec.get("items") or []):
        if item["visual"] == "text":
            continue
        try:
            results[item["id"]] = dashboards.data_for_spec(
                db, settings, runtime, spec, item["id"], None, user=viewer, include_query=False
            )
        except ApiError as exc:
            results[item["id"]] = exc.message
    message = render(settings, dashboard.id, spec, results, user.display_name, row.cron)
    try:
        mail.send(settings, user.email, message)
    except mail.MailError as exc:
        raise JobError(exc.message) from None
    return {"sent": True, "to": user.email, "items": len(results)}


# ---- rendering ----------------------------------------------------------------------


def _reading_order(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(items, key=lambda i: (i["position"]["y"], i["position"]["x"]))


def _fiscal_year(year: int, month: int, calendar: CalendarConfig) -> str:
    start = calendar.fiscal_year_start_month
    start_year = year if month >= start else year - 1
    end_year = start_year if start == 1 else start_year + 1
    if calendar.fiscal_year_label == "fy_end_year":
        return f"FY{end_year}"
    if start_year == end_year:
        return str(end_year)
    return f"{start_year}-{end_year % 100:02d}"


def period_label(value: Any, grain: str | None, calendar: CalendarConfig) -> str:
    """The same period names as the app (frontend/src/lib/calendar.ts)."""
    text = str(value)
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return text
    year, month, day = (int(part) for part in match.groups())
    name = MONTHS[month - 1]
    into_fy = (month - calendar.fiscal_year_start_month) % 12
    labels = {
        "day": f"{day} {name} {year}",
        "week": f"Week of {day} {name} {year}",
        "month": f"{name} {year}",
        "quarter": f"Q{(month - 1) // 3 + 1} {year}",
        "year": str(year),
        "fiscal_month": f"{name} {year} (P{into_fy + 1})",
        "fiscal_quarter": f"Q{into_fy // 3 + 1} {_fiscal_year(year, month, calendar)}",
        "fiscal_year": _fiscal_year(year, month, calendar),
    }
    return labels.get(grain or "", text[:10] if grain else text.replace("T", " "))


def format_measure(value: Any, fmt: str | None) -> str:
    """Like the app's formatMeasure: en-AU numbers, currency and percent."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if fmt and fmt.startswith("currency_"):
        code = fmt.removeprefix("currency_").upper()
        if re.fullmatch(r"[A-Z]{3}", code):
            # en-AU writes its own dollar as $ and every other currency by its code.
            symbol = "$" if code == "AUD" else f"{code} "
            sign = "-" if value < 0 else ""
            return f"{sign}{symbol}{abs(value):,.2f}"
    if fmt == "percent":
        return f"{value * 100:,.1f}".rstrip("0").rstrip(".") + "%"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _cell(value: Any, column: Mapping[str, Any], calendar: CalendarConfig) -> str:
    if value is None:
        return ""
    if column.get("role") == "measure":
        return format_measure(value, column.get("format"))
    if column.get("role") == "time" or column.get("type") in ("date", "timestamp"):
        return period_label(value, column.get("grain"), calendar)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format_measure(value, None)
    return str(value)


def _label(column: Mapping[str, Any]) -> str:
    return str(column.get("label") or str(column["name"]).partition(".")[2] or column["name"])


def _markdown(text: str) -> str:
    """A text item's Markdown as simple HTML: headings, paragraphs and bullets."""
    blocks: list[str] = []
    paragraph: list[str] = []

    def inline(line: str) -> str:
        safe = escape(line)
        safe = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", safe)
        safe = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m[1] or m[2]}</b>", safe)
        return safe

    def flush() -> None:
        if paragraph:
            blocks.append(f'<p style="margin:0 0 10px">{" ".join(paragraph)}</p>')
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^[-*+]\s+(.*)$", line)
        if not line:
            flush()
        elif heading:
            flush()
            size = 17 if len(heading[1]) <= 2 else 15
            blocks.append(
                f'<p style="margin:16px 0 6px;font-size:{size}px;font-weight:600">'
                f"{inline(heading[2])}</p>"
            )
        elif bullet:
            flush()
            blocks.append(f'<p style="margin:0 0 4px 12px">&bull; {inline(bullet[1])}</p>')
        else:
            paragraph.append(inline(line))
    flush()
    return "".join(blocks)


def _kpi(body: Mapping[str, Any]) -> tuple[str, str]:
    columns = body.get("columns") or []
    rows = body.get("rows") or []
    value = rows[0][0] if rows and rows[0] else None
    fmt = columns[0].get("format") if columns else None
    shown = format_measure(value, fmt) if value is not None else "No data"
    html = f'<p style="margin:0;font-size:26px;font-weight:600">{escape(shown)}</p>'
    text = shown
    comparison = body.get("comparison")
    if comparison and isinstance(value, int | float):
        before_rows = comparison.get("rows") or []
        before = before_rows[0][0] if before_rows and before_rows[0] else None
        what = "the previous year" if comparison.get("mode") == "previous_year" else "before"
        if isinstance(before, int | float) and before:
            change = (value - before) / abs(before)
            line = f"{change:+.1%} on {what} ({format_measure(before, fmt)})"
        else:
            line = f"Nothing to compare with {what}"
        html += f'<p style="margin:2px 0 0;color:{MUTED};font-size:13px">{escape(line)}</p>'
        text += f" ({line})"
    return html, text


def _table(body: Mapping[str, Any], visual: str, calendar: CalendarConfig) -> tuple[str, str]:
    columns = list(body.get("columns") or [])
    rows = list(body.get("rows") or [])
    if not rows:
        return f'<p style="margin:0;color:{MUTED}">No rows.</p>', "No rows."
    shown = rows[:ROWS_SHOWN]
    # Charts get a bar for their first measure, so the email still reads as a chart.
    bar_at = (
        next((i for i, c in enumerate(columns) if c.get("role") == "measure"), None)
        if visual not in ("table", "scatter")
        else None
    )
    values = [r[bar_at] for r in shown] if bar_at is not None else []
    if not values or not all(isinstance(v, int | float) and v >= 0 for v in values):
        bar_at = None
    top = max(values) if bar_at is not None and values else 0
    head = "".join(
        f'<th style="text-align:{"right" if c.get("role") == "measure" else "left"};'
        f'padding:4px 8px;border-bottom:1px solid {LINE};color:{MUTED};font-weight:600">'
        f"{escape(_label(c))}</th>"
        for c in columns
    )
    if bar_at is not None:
        head += f'<th style="padding:4px 8px;border-bottom:1px solid {LINE}"></th>'
    lines = []
    text_rows = [" | ".join(_label(c) for c in columns)]
    for row in shown:
        cells = [_cell(v, c, calendar) for v, c in zip(row, columns, strict=False)]
        text_rows.append(" | ".join(cells))
        html_cells = "".join(
            f'<td style="text-align:{"right" if c.get("role") == "measure" else "left"};'
            f'padding:4px 8px;border-bottom:1px solid {LINE}">{escape(cell)}</td>'
            for cell, c in zip(cells, columns, strict=False)
        )
        if bar_at is not None:
            width = max(1, round(120 * (row[bar_at] / top))) if top else 1
            html_cells += (
                f'<td style="padding:4px 8px;border-bottom:1px solid {LINE}">'
                f'<div style="background:{BAR};height:8px;width:{width}px;border-radius:2px">'
                "</div></td>"
            )
        lines.append(f"<tr>{html_cells}</tr>")
    html = (
        '<table role="presentation" style="border-collapse:collapse;font-size:13px">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(lines)}</tbody></table>"
    )
    text = "\n".join(text_rows)
    total = int(body.get("rowCount") or len(rows))
    if total > len(shown) or body.get("truncated"):
        more = f"The first {len(shown)} of {total}{'+' if body.get('truncated') else ''} rows."
        html += f'<p style="margin:4px 0 0;color:{MUTED};font-size:12px">{escape(more)}</p>'
        text += f"\n{more}"
    return html, text


def _when(cron: str) -> str:
    preset = next((p for p in schedules.PRESETS if p["cron"] == cron), None)
    return preset["label"] if preset else f"cron {cron}"


def render(
    settings: Settings,
    dashboard_id: uuid.UUID,
    spec: Mapping[str, Any],
    results: Mapping[str, dict[str, Any] | str],
    name: str,
    cron: str,
    now: datetime | None = None,
) -> mail.Message:
    """The email: each item in reading order, then a link to the dashboard."""
    calendar = settings.calendar
    zone = ZoneInfo(settings.workspace_timezone)
    at = (now or datetime.now(UTC)).astimezone(zone)
    title = str(spec.get("title") or "Dashboard")
    link = f"{settings.public_base_url}/dashboards/{dashboard_id}"
    when = f"{at.day} {MONTHS[at.month - 1]} {at.year}, {at:%H:%M}"
    intro = f"{title}, as {name} sees it on {when}, with the filters at their defaults."
    html = [
        f'<p style="margin:0 0 4px;font-size:20px;font-weight:600">{escape(title)}</p>',
        f'<p style="margin:0 0 16px;color:{MUTED};font-size:13px">{escape(intro)}</p>',
    ]
    text = [intro, ""]
    for item in _reading_order(spec.get("items") or []):
        heading = str(item.get("title") or "")
        if item["visual"] == "text":
            markdown = str((item.get("config") or {}).get("markdown") or "")
            html.append(f'<div style="margin:16px 0 0">{_markdown(markdown)}</div>')
            text += [markdown, ""]
            continue
        result = results.get(item["id"])
        if heading:
            html.append(
                f'<p style="margin:20px 0 6px;font-size:15px;font-weight:600">{escape(heading)}</p>'
            )
            text.append(heading)
        elif item["visual"] != "kpi":
            html.append('<div style="height:16px"></div>')
        if isinstance(result, str) or result is None:
            problem = result or "This visual could not be loaded."
            html.append(f'<p style="margin:0;color:{MUTED}">{escape(problem)}</p>')
            text += [problem, ""]
        elif result.get("empty"):
            html.append(f'<p style="margin:0;color:{MUTED}">This visual has no fields yet.</p>')
            text += ["This visual has no fields yet.", ""]
        elif item["visual"] == "kpi":
            part_html, part_text = _kpi(result)
            if not heading:
                html.append('<div style="height:12px"></div>')
            html.append(part_html)
            text += [part_text, ""]
        else:
            part_html, part_text = _table(result, item["visual"], calendar)
            html.append(part_html)
            text += [part_text, ""]
    html.append(
        f'<p style="margin:24px 0"><a href="{escape(link, quote=True)}" style="{mail.BUTTON}">'
        "Open the dashboard</a></p>"
    )
    footer = (
        f"You asked for this email: {_when(cron)} ({settings.workspace_timezone}). To stop "
        "it, open the dashboard and choose Email me, or go to your account page."
    )
    html.append(f'<p style="margin:0;color:{MUTED};font-size:12px">{escape(footer)}</p>')
    text += [link, "", footer]
    page = mail.page(settings, html)
    return mail.Message(
        subject=f"{title} ({at.day} {MONTHS[at.month - 1]} {at.year})",
        text="\n".join(text).strip() + "\n",
        html=page,
    )
