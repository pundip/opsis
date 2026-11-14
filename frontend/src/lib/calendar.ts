// Frontend half of the calendar rules in backend/opsis/semantic/calendar.py (ADR-012).
// The backend returns period start dates; labels are formatted here.
import type { AppConfig } from "./config";

type Calendar = AppConfig["calendar"];

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export function monthName(month: number): string {
  return MONTHS[month - 1] ?? `Month ${month}`;
}

/** Label of the fiscal year containing a date. year and month are 1-based calendar values. */
export function fiscalYearLabel(year: number, month: number, calendar: Calendar): string {
  const start = calendar.fiscalYearStartMonth;
  const startYear = month >= start ? year : year - 1;
  const endYear = start === 1 ? startYear : startYear + 1;
  if (calendar.fiscalYearLabel === "fy_end_year") return `FY${endYear}`;
  if (startYear === endYear) return String(endYear);
  return `${startYear}-${String(endYear % 100).padStart(2, "0")}`;
}

const SHORT_MONTHS = MONTHS.map((name) => name.slice(0, 3));

/**
 * Display label for a period, given its start date ("2026-10-01") and grain.
 * The backend always returns period start dates; this is where they get names.
 */
export function periodLabel(startIso: string, grain: string, calendar: Calendar): string {
  const [year, month, day] = startIso.slice(0, 10).split("-").map(Number) as [number, number, number];
  const monthName = SHORT_MONTHS[month - 1] ?? "";
  const fiscalYear = () => fiscalYearLabel(year, month, calendar);
  const monthsIntoFiscalYear = (month - calendar.fiscalYearStartMonth + 12) % 12;
  switch (grain) {
    case "day":
      return `${day} ${monthName} ${year}`;
    case "week":
      return `Week of ${day} ${monthName} ${year}`;
    case "month":
      return `${monthName} ${year}`;
    case "quarter":
      return `Q${Math.floor((month - 1) / 3) + 1} ${year}`;
    case "year":
      return String(year);
    case "fiscal_month":
      return `${monthName} ${year} (P${monthsIntoFiscalYear + 1})`;
    case "fiscal_quarter":
      return `Q${Math.floor(monthsIntoFiscalYear / 3) + 1} ${fiscalYear()}`;
    case "fiscal_year":
      return fiscalYear();
    default:
      return startIso;
  }
}

/** Today's year and month in the workspace timezone. */
export function todayIn(timezone: string, now = new Date()): { year: number; month: number } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "numeric",
  }).formatToParts(now);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return { year: get("year"), month: get("month") };
}
