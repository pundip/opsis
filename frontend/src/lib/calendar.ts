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
