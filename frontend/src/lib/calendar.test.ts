import { describe, expect, it } from "vitest";
import { fiscalYearLabel, monthName, todayIn } from "./calendar";

const base = { timezone: "Australia/Melbourne", weekStart: "monday" as const };
const july = { ...base, fiscalYearStartMonth: 7, fiscalYearLabel: "fy_end_year" as const };

describe("fiscalYearLabel", () => {
  it("names the year by its end year by default", () => {
    expect(fiscalYearLabel(2026, 11, july)).toBe("FY2027");
    expect(fiscalYearLabel(2026, 6, july)).toBe("FY2026");
    expect(fiscalYearLabel(2026, 7, july)).toBe("FY2027");
  });

  it("supports the year span style", () => {
    const span = { ...july, fiscalYearLabel: "year_span" as const };
    expect(fiscalYearLabel(2026, 11, span)).toBe("2026-27");
    expect(fiscalYearLabel(2026, 6, span)).toBe("2025-26");
    expect(fiscalYearLabel(2099, 8, span)).toBe("2099-00");
  });

  it("matches the calendar year when the fiscal year starts in January", () => {
    expect(fiscalYearLabel(2026, 3, { ...july, fiscalYearStartMonth: 1 })).toBe("FY2026");
    expect(
      fiscalYearLabel(2026, 3, { ...july, fiscalYearStartMonth: 1, fiscalYearLabel: "year_span" }),
    ).toBe("2026");
  });
});

describe("helpers", () => {
  it("names months", () => {
    expect(monthName(7)).toBe("July");
  });

  it("finds today in the workspace timezone", () => {
    // 30 June 2026 23:30 UTC is already 1 July in Melbourne.
    const now = new Date("2026-06-30T23:30:00Z");
    expect(todayIn("Australia/Melbourne", now)).toEqual({ year: 2026, month: 7 });
    expect(todayIn("UTC", now)).toEqual({ year: 2026, month: 6 });
  });
});
