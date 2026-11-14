"""Calendar rules shared by the compiler and the UI labels: grains and relative ranges.

Pure functions, no I/O. The compiler (M3) uses `period_start` semantics when it
truncates dates and `resolve_relative_range` when it turns a relative range
into literal dates. Everything is driven by three workspace settings:
WORKSPACE_TIMEZONE (decides "today"), WEEK_START and FISCAL_YEAR_START_MONTH.

Conventions (ADR-012):
- Every grain is identified by the first date of its period.
- A fiscal year is named after the calendar year it ends in. With a July start,
  FY2027 runs 2026-07-01 to 2027-06-30. With a January start it equals the
  calendar year.
- Ranges are inclusive of both ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Grain = Literal[
    "day", "week", "month", "quarter", "year", "fiscal_month", "fiscal_quarter", "fiscal_year"
]
WeekStart = Literal["monday", "sunday"]
FiscalYearLabel = Literal["fy_end_year", "year_span"]

_UNITS = "week|month|quarter|year|fiscal_quarter|fiscal_year"
_RELATIVE = re.compile(
    rf"^(?:(?P<fixed>today|yesterday|all_time)"
    rf"|(?P<which>this|last)_(?P<unit>{_UNITS})"
    rf"|last_(?P<n>[1-9][0-9]{{0,3}})_(?P<units>day|{_UNITS})s"
    rf"|(?P<todate>{_UNITS})_to_date)$"
)


@dataclass(frozen=True)
class CalendarConfig:
    week_start: WeekStart = "monday"
    fiscal_year_start_month: int = 7
    fiscal_year_label: FiscalYearLabel = "fy_end_year"

    def __post_init__(self) -> None:
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be between 1 and 12")


def _add_months(d: date, months: int) -> date:
    """Shift a first-of-month date by a number of months."""
    index = d.year * 12 + (d.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def period_start(d: date, grain: Grain, cfg: CalendarConfig) -> date:
    """First date of the period that contains `d`."""
    if grain == "day":
        return d
    if grain == "week":
        offset = d.weekday() if cfg.week_start == "monday" else (d.weekday() + 1) % 7
        return d - timedelta(days=offset)
    month_start = d.replace(day=1)
    if grain in ("month", "fiscal_month"):
        return month_start
    if grain == "quarter":
        return _add_months(month_start, -((d.month - 1) % 3))
    if grain == "year":
        return date(d.year, 1, 1)
    months_into_fy = (d.month - cfg.fiscal_year_start_month) % 12
    if grain == "fiscal_quarter":
        return _add_months(month_start, -(months_into_fy % 3))
    if grain == "fiscal_year":
        return _add_months(month_start, -months_into_fy)
    raise ValueError(f"Unknown grain: {grain}")


def shift_period(start: date, grain: Grain, count: int) -> date:
    """Move a period start forwards or backwards by whole periods."""
    if grain == "day":
        return start + timedelta(days=count)
    if grain == "week":
        return start + timedelta(weeks=count)
    months = {"month": 1, "fiscal_month": 1, "quarter": 3, "fiscal_quarter": 3}.get(grain, 12)
    return _add_months(start, months * count)


def period_end(start: date, grain: Grain) -> date:
    """Last date of the period that begins at `start`."""
    return shift_period(start, grain, 1) - timedelta(days=1)


def fiscal_year_of(d: date, cfg: CalendarConfig) -> int:
    """The fiscal year number for a date (named after the year it ends in)."""
    start = period_start(d, "fiscal_year", cfg)
    return period_end(start, "fiscal_year").year


def fiscal_year_label(d: date, cfg: CalendarConfig) -> str:
    """Display name of the fiscal year containing `d`.

    fy_end_year: "FY2027". year_span: "2026-27", or just "2026" when the fiscal
    year is the calendar year.
    """
    start = period_start(d, "fiscal_year", cfg)
    end_year = period_end(start, "fiscal_year").year
    if cfg.fiscal_year_label == "fy_end_year":
        return f"FY{end_year}"
    if start.year == end_year:
        return str(end_year)
    return f"{start.year}-{end_year % 100:02d}"


def fiscal_period_of(d: date, cfg: CalendarConfig) -> int:
    """Month number within the fiscal year, 1 to 12."""
    return (d.month - cfg.fiscal_year_start_month) % 12 + 1


def resolve_relative_range(
    relative: str, today: date, cfg: CalendarConfig
) -> tuple[date | None, date | None]:
    """Turn a relative range name into inclusive (from, to) dates.

    `today` must already be the current date in the workspace timezone.
    all_time returns (None, None).

    - this_<unit>: the whole current period, including its future part.
    - last_<unit>: the previous complete period.
    - last_N_<units>: N periods ending with the current one, up to today.
    - <unit>_to_date: start of the current period up to today.
    """
    match = _RELATIVE.match(relative)
    if match is None:
        raise ValueError(f"Unknown relative range: {relative!r}")
    if match["fixed"] == "today":
        return today, today
    if match["fixed"] == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if match["fixed"] == "all_time":
        return None, None
    if match["which"]:
        grain: Grain = match["unit"]  # type: ignore[assignment]
        start = period_start(today, grain, cfg)
        if match["which"] == "last":
            start = shift_period(start, grain, -1)
        return start, period_end(start, grain)
    if match["n"]:
        grain = match["units"]  # type: ignore[assignment]
        start = shift_period(period_start(today, grain, cfg), grain, -(int(match["n"]) - 1))
        return start, today
    grain = match["todate"]  # type: ignore[assignment]
    return period_start(today, grain, cfg), today
