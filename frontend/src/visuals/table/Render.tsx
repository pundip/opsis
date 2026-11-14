import { useMemo, useState } from "react";
import { displayValue } from "../../lib/chartOptions";
import { formatMeasure } from "../../lib/format";
import type { ResultColumn } from "../../lib/semantic";
import type { RenderProps } from "../types";

interface Rule {
  field: string;
  operator: "gt" | "gte" | "lt" | "lte" | "eq";
  value: number;
  style?: "good" | "bad" | "highlight";
}

const TESTS: Record<Rule["operator"], (a: number, b: number) => boolean> = {
  gt: (a, b) => a > b,
  gte: (a, b) => a >= b,
  lt: (a, b) => a < b,
  lte: (a, b) => a <= b,
  eq: (a, b) => a === b,
};

// Conditional formats pair a background with a marker, so they do not rely on colour alone.
const STYLES: Record<NonNullable<Rule["style"]>, { className: string; marker: string }> = {
  good: { className: "bg-emerald-100 dark:bg-emerald-950", marker: "▲ " },
  bad: { className: "bg-red-100 dark:bg-red-950", marker: "▼ " },
  highlight: { className: "bg-amber-100 dark:bg-amber-950", marker: "● " },
};

function compare(a: unknown, b: unknown): number {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : 1; // blanks last
  if (b === null || b === undefined) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

export default function Render({ data, config, theme, calendar, onSelect }: RenderProps) {
  const [sort, setSort] = useState<{ index: number; desc: boolean } | null>(null);
  const [page, setPage] = useState(0);
  const pageSize = Math.max(1, Number(config.pageSize ?? 15));
  const rules = (Array.isArray(config.conditionalFormatting) ? config.conditionalFormatting : []) as Rule[];

  const rows = useMemo(() => {
    if (!sort) return data.rows;
    const sorted = [...data.rows].sort((a, b) => compare(a[sort.index], b[sort.index]));
    return sort.desc ? sorted.reverse() : sorted;
  }, [data.rows, sort]);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(page, pages - 1);
  const visible = rows.slice(current * pageSize, (current + 1) * pageSize);

  const text = (value: unknown, column: ResultColumn) =>
    column.role === "measure" ? formatMeasure(value, column.format) : displayValue(value, column, calendar);
  const styleFor = (value: unknown, column: ResultColumn) => {
    if (typeof value !== "number") return undefined;
    const rule = rules.find((r) => r.field === column.name && TESTS[r.operator]?.(value, r.value));
    return rule ? STYLES[rule.style ?? "highlight"] : undefined;
  };

  return (
    <div className="flex h-full flex-col text-sm" style={{ color: theme.ink }}>
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-separate border-spacing-0">
          <thead>
            <tr>
              {data.columns.map((column, index) => {
                const active = sort?.index === index;
                return (
                  <th
                    key={column.name}
                    scope="col"
                    aria-sort={active ? (sort.desc ? "descending" : "ascending") : "none"}
                    className={`sticky top-0 border-b px-2 py-1.5 text-xs font-medium whitespace-nowrap ${column.role === "measure" ? "text-right" : "text-left"}`}
                    style={{ background: theme.surface, borderColor: theme.grid, color: theme.inkSecondary }}
                  >
                    <button
                      type="button"
                      className="hover:underline"
                      title="Sort by this column"
                      onClick={() => {
                        setPage(0);
                        setSort(active ? (sort.desc ? null : { index, desc: true }) : { index, desc: false });
                      }}
                    >
                      {column.label ?? column.name}
                      {active ? (sort.desc ? " ▼" : " ▲") : ""}
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, index) => {
                  const column = data.columns[index]!;
                  const format = styleFor(cell, column);
                  const clickable = onSelect && column.role === "dimension" && cell !== null;
                  return (
                    <td
                      key={index}
                      className={`border-b px-2 py-1 whitespace-nowrap ${column.role === "measure" ? "text-right tabular-nums" : ""} ${format?.className ?? ""}`}
                      style={{ borderColor: theme.grid }}
                    >
                      {clickable ? (
                        <button
                          type="button"
                          className="text-left hover:underline"
                          title="Filter the dashboard to this value"
                          onClick={() => onSelect(column.name, cell, text(cell, column))}
                        >
                          {text(cell, column)}
                        </button>
                      ) : (
                        <>
                          {format?.marker}
                          {text(cell, column)}
                        </>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="flex items-center justify-end gap-2 pt-1 text-xs" style={{ color: theme.inkSecondary }}>
          <span>
            Rows {current * pageSize + 1} to {Math.min(rows.length, (current + 1) * pageSize)} of {rows.length}
            {data.truncated && " (more exist)"}
          </span>
          <button type="button" className="rounded border px-2 py-0.5 disabled:opacity-40" style={{ borderColor: theme.axis }} disabled={current === 0} onClick={() => setPage(current - 1)}>
            Previous
          </button>
          <button type="button" className="rounded border px-2 py-0.5 disabled:opacity-40" style={{ borderColor: theme.axis }} disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}
