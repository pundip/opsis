"""The built-in date connector: one row per day, for a date table (V2_DESIGN 1.3).

It reads nothing outside Opsis and holds no secrets. Its one stream, `dates`, runs
from `start` to `end`, or with no `end` to the last day of the next financial year,
so a table refreshed every day always reaches past today. The calendar columns follow
the workspace calendar (WEEK_START, FISCAL_YEAR_START_MONTH, FISCAL_YEAR_LABEL and
WORKSPACE_TIMEZONE for "today"), which the app passes in as `_calendar`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow as pa

from opsis.connectors.base import (
    ColumnSpec,
    ColumnType,
    ConnectorError,
    ReadOptions,
    ReadResult,
    RefreshMode,
    StreamSpec,
    TestResult,
)
from opsis.connectors.tabular import rows_to_batch
from opsis.semantic.calendar import (
    CalendarConfig,
    fiscal_period_of,
    fiscal_year_label,
    fiscal_year_of,
    period_end,
    period_start,
    shift_period,
)

STREAM = "dates"
MAX_DAYS = 100 * 366  # a century: a longer table is a mistake in the first or last date
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _column(name: str, kind: ColumnType) -> ColumnSpec:
    return ColumnSpec(name, kind, nullable=False)


COLUMNS = (
    _column("date", ColumnType.DATE),
    _column("year", ColumnType.INTEGER),
    _column("quarter", ColumnType.INTEGER),
    _column("quarter_label", ColumnType.STRING),  # Q1
    _column("month", ColumnType.INTEGER),
    _column("month_name", ColumnType.STRING),  # January
    _column("month_short", ColumnType.STRING),  # Jan
    _column("year_month", ColumnType.STRING),  # 2026-01
    _column("week_start", ColumnType.DATE),  # the first day of its week
    _column("day_of_month", ColumnType.INTEGER),
    _column("day_of_week", ColumnType.INTEGER),  # 1 is the first day of the week
    _column("day_name", ColumnType.STRING),  # Monday
    _column("day_short", ColumnType.STRING),  # Mon
    _column("day_of_year", ColumnType.INTEGER),
    _column("is_weekend", ColumnType.BOOLEAN),
    _column("fiscal_year", ColumnType.INTEGER),  # named after the year it ends in
    _column("fiscal_year_label", ColumnType.STRING),  # FY2027, or 2026-27
    _column("fiscal_quarter", ColumnType.INTEGER),
    _column("fiscal_month", ColumnType.INTEGER),  # 1 is the first month of the year
)


def _calendar(given: Any) -> CalendarConfig:
    if not isinstance(given, Mapping):
        return CalendarConfig()
    label = given.get("fiscalYearLabel")
    week = given.get("weekStart")
    return CalendarConfig(
        week_start="sunday" if week == "sunday" else "monday",
        fiscal_year_start_month=int(given.get("fiscalYearStartMonth") or 7),
        fiscal_year_label="year_span" if label == "year_span" else "fy_end_year",
        timezone=str(given["timezone"]) if given.get("timezone") else None,
    )


def _parse(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConnectorError(f"The {what} date must be a date such as 2020-07-01.") from None


def row(day: date, calendar: CalendarConfig) -> tuple[Any, ...]:
    """The values of one day, in the order of COLUMNS."""
    weekday = day.weekday()  # Monday is 0
    first_day = 0 if calendar.week_start == "monday" else 6
    fiscal_month = fiscal_period_of(day, calendar)
    quarter = (day.month - 1) // 3 + 1
    return (
        day,
        day.year,
        quarter,
        f"Q{quarter}",
        day.month,
        MONTHS[day.month - 1],
        MONTHS[day.month - 1][:3],
        f"{day.year:04d}-{day.month:02d}",
        period_start(day, "week", calendar),
        day.day,
        (weekday - first_day) % 7 + 1,
        DAYS[weekday],
        DAYS[weekday][:3],
        day.timetuple().tm_yday,
        weekday >= 5,
        fiscal_year_of(day, calendar),
        fiscal_year_label(day, calendar),
        (fiscal_month - 1) // 3 + 1,
        fiscal_month,
    )


class DateConnector:
    id: ClassVar[str] = "date"
    name: ClassVar[str] = "Date table"
    version: ClassVar[str] = "1.0.0"

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["start"],
            "properties": {
                "start": {
                    "type": "string",
                    "title": "First date",
                    "pattern": DATE_PATTERN,
                    "description": "The first day in the table, such as 2020-07-01.",
                },
                "end": {
                    "type": "string",
                    "title": "Last date",
                    "pattern": DATE_PATTERN,
                    "description": "Leave empty to end with the next financial year, so the "
                    "table grows as the years go by.",
                },
            },
        }

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.calendar = _calendar(config.get("_calendar"))

    def today(self) -> date:
        if self.calendar.timezone:
            try:
                return datetime.now(ZoneInfo(self.calendar.timezone)).date()
            except ZoneInfoNotFoundError:
                pass
        return date.today()

    def span(self) -> tuple[date, date]:
        """The first and last days of the table."""
        start = _parse(self.config.get("start"), "first")
        if self.config.get("end"):
            end = _parse(self.config["end"], "last")
        else:
            this_year = period_start(self.today(), "fiscal_year", self.calendar)
            end = period_end(shift_period(this_year, "fiscal_year", 1), "fiscal_year")
        if end < start:
            raise ConnectorError(f"The last date ({end}) is before the first date ({start}).")
        if (end - start).days + 1 > MAX_DAYS:
            raise ConnectorError(
                f"From {start} to {end} is more than {MAX_DAYS:,} days. Choose a later first "
                "date or an earlier last date."
            )
        return start, end

    def test_connection(self) -> TestResult:
        try:
            start, end = self.span()
        except ConnectorError as exc:
            return TestResult(ok=False, message=str(exc))
        days = (end - start).days + 1
        return TestResult(ok=True, message=f"Dates from {start} to {end} ({days:,} days).")

    def discover(self) -> list[StreamSpec]:
        start, end = self.span()
        return [
            StreamSpec(
                name=STREAM,
                columns=COLUMNS,
                label="Dates",
                primary_key=("date",),
                supported_modes=(RefreshMode.FULL,),
                row_count_estimate=(end - start).days + 1,
            )
        ]

    def read(
        self,
        stream: str,
        mode: RefreshMode,
        state: dict[str, Any] | None,
        options: ReadOptions | None = None,
    ) -> ReadResult:
        if stream != STREAM:
            raise ConnectorError(f"The date connector has one table, {STREAM!r}, not {stream!r}.")
        options = options or ReadOptions()
        start, end = self.span()
        keep = [
            i for i, c in enumerate(COLUMNS) if not options.columns or c.name in options.columns
        ]
        columns = [COLUMNS[i] for i in keep]

        def batches() -> Iterator[pa.RecordBatch]:
            day, chunk = start, []
            while day <= end:
                values = row(day, self.calendar)
                chunk.append(tuple(values[i] for i in keep))
                if len(chunk) >= options.batch_size:
                    yield rows_to_batch(columns, chunk, options.conversion)
                    chunk = []
                day += timedelta(days=1)
            if chunk:
                yield rows_to_batch(columns, chunk, options.conversion)

        return ReadResult(batches())

    def close(self) -> None:
        return None


CONNECTOR = DateConnector
