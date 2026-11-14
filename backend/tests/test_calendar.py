from __future__ import annotations

from datetime import date

import pytest

from opsis.semantic.calendar import (
    CalendarConfig,
    fiscal_period_of,
    fiscal_year_label,
    fiscal_year_of,
    period_end,
    period_start,
    resolve_relative_range,
)

AU = CalendarConfig(week_start="monday", fiscal_year_start_month=7)
TODAY = date(2026, 11, 14)  # a Saturday, in FY2027 Q2


@pytest.mark.parametrize(
    ("grain", "expected"),
    [
        ("day", date(2026, 11, 14)),
        ("week", date(2026, 11, 9)),
        ("month", date(2026, 11, 1)),
        ("quarter", date(2026, 10, 1)),
        ("year", date(2026, 1, 1)),
        ("fiscal_month", date(2026, 11, 1)),
        ("fiscal_quarter", date(2026, 10, 1)),
        ("fiscal_year", date(2026, 7, 1)),
    ],
)
def test_period_start(grain: str, expected: date) -> None:
    assert period_start(TODAY, grain, AU) == expected  # type: ignore[arg-type]


def test_fiscal_year_boundaries() -> None:
    assert period_start(date(2026, 6, 30), "fiscal_year", AU) == date(2025, 7, 1)
    assert period_start(date(2026, 7, 1), "fiscal_year", AU) == date(2026, 7, 1)
    assert period_end(date(2026, 7, 1), "fiscal_year") == date(2027, 6, 30)
    assert fiscal_year_of(date(2026, 6, 30), AU) == 2026
    assert fiscal_year_of(date(2026, 7, 1), AU) == 2027
    assert fiscal_period_of(date(2026, 7, 15), AU) == 1
    assert fiscal_period_of(date(2026, 6, 15), AU) == 12


def test_fiscal_year_labels() -> None:
    span = CalendarConfig(fiscal_year_start_month=7, fiscal_year_label="year_span")
    assert fiscal_year_label(date(2026, 11, 14), AU) == "FY2027"
    assert fiscal_year_label(date(2026, 6, 30), AU) == "FY2026"
    assert fiscal_year_label(date(2026, 11, 14), span) == "2026-27"
    assert fiscal_year_label(date(2026, 6, 30), span) == "2025-26"
    assert fiscal_year_label(date(2099, 8, 1), span) == "2099-00"
    calendar_year = CalendarConfig(fiscal_year_start_month=1, fiscal_year_label="year_span")
    assert fiscal_year_label(date(2026, 3, 1), calendar_year) == "2026"
    assert (
        fiscal_year_label(date(2026, 3, 1), CalendarConfig(fiscal_year_start_month=1)) == "FY2026"
    )


def test_fiscal_quarters_follow_the_start_month() -> None:
    april = CalendarConfig(fiscal_year_start_month=4)
    assert period_start(date(2026, 3, 31), "fiscal_quarter", april) == date(2026, 1, 1)
    assert period_start(date(2026, 4, 1), "fiscal_year", april) == date(2026, 4, 1)
    feb = CalendarConfig(fiscal_year_start_month=2)
    assert period_start(date(2026, 1, 20), "fiscal_quarter", feb) == date(2025, 11, 1)
    assert period_start(date(2026, 1, 20), "fiscal_year", feb) == date(2025, 2, 1)


def test_january_start_equals_the_calendar_year() -> None:
    cal = CalendarConfig(fiscal_year_start_month=1)
    for d in (date(2026, 1, 1), date(2026, 8, 19), date(2026, 12, 31)):
        assert period_start(d, "fiscal_year", cal) == period_start(d, "year", cal)
        assert period_start(d, "fiscal_quarter", cal) == period_start(d, "quarter", cal)
        assert fiscal_year_of(d, cal) == d.year


def test_week_start_sunday() -> None:
    sunday = CalendarConfig(week_start="sunday")
    assert period_start(TODAY, "week", sunday) == date(2026, 11, 8)
    assert period_start(date(2026, 11, 8), "week", sunday) == date(2026, 11, 8)
    assert period_start(date(2026, 11, 8), "week", AU) == date(2026, 11, 2)


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("today", (date(2026, 11, 14), date(2026, 11, 14))),
        ("yesterday", (date(2026, 11, 13), date(2026, 11, 13))),
        ("all_time", (None, None)),
        ("this_month", (date(2026, 11, 1), date(2026, 11, 30))),
        ("last_month", (date(2026, 10, 1), date(2026, 10, 31))),
        ("last_week", (date(2026, 11, 2), date(2026, 11, 8))),
        ("this_year", (date(2026, 1, 1), date(2026, 12, 31))),
        ("last_quarter", (date(2026, 7, 1), date(2026, 9, 30))),
        ("this_fiscal_year", (date(2026, 7, 1), date(2027, 6, 30))),
        ("last_fiscal_year", (date(2025, 7, 1), date(2026, 6, 30))),
        ("this_fiscal_quarter", (date(2026, 10, 1), date(2026, 12, 31))),
        ("last_fiscal_quarter", (date(2026, 7, 1), date(2026, 9, 30))),
        ("fiscal_year_to_date", (date(2026, 7, 1), date(2026, 11, 14))),
        ("year_to_date", (date(2026, 1, 1), date(2026, 11, 14))),
        ("month_to_date", (date(2026, 11, 1), date(2026, 11, 14))),
        ("last_30_days", (date(2026, 10, 16), date(2026, 11, 14))),
        ("last_1_days", (date(2026, 11, 14), date(2026, 11, 14))),
        ("last_12_months", (date(2025, 12, 1), date(2026, 11, 14))),
        ("last_2_fiscal_years", (date(2025, 7, 1), date(2026, 11, 14))),
        ("last_4_quarters", (date(2026, 1, 1), date(2026, 11, 14))),
    ],
)
def test_relative_ranges(relative: str, expected: tuple[date | None, date | None]) -> None:
    assert resolve_relative_range(relative, TODAY, AU) == expected


@pytest.mark.parametrize("bad", ["last_fortnight", "last_0_days", "this_day", "", "last_3_month"])
def test_unknown_relative_range(bad: str) -> None:
    with pytest.raises(ValueError, match="Unknown relative range"):
        resolve_relative_range(bad, TODAY, AU)
