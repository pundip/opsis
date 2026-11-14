import { compactNumber } from "../../lib/chartOptions";
import { formatMeasure } from "../../lib/format";
import type { RenderProps } from "../types";

const COMPARED_WITH: Record<string, string> = {
  previous_period: "the previous period",
  previous_year: "the same period last year",
};

function headline(value: unknown, format: string | null, compact: boolean): string {
  if (typeof value !== "number") return "No data";
  if (!compact || Math.abs(value) < 10_000) return formatMeasure(value, format);
  const short = compactNumber(value);
  return format?.startsWith("currency_") ? formatMeasure(0, format).replace(/[\d.,]+/, short) : short;
}

export default function Render({ data, config, theme }: RenderProps) {
  const column = data.columns[0];
  const value = data.rows[0]?.[0];
  const previous = data.comparison?.rows[0]?.[0];
  const change =
    typeof value === "number" && typeof previous === "number" && previous !== 0
      ? (value - previous) / Math.abs(previous)
      : null;
  const good = change !== null && change !== 0 ? change > 0 === (config.upIsGood !== false) : null;
  return (
    <div className="flex h-full flex-col justify-center">
      <p className="text-3xl leading-tight font-semibold" style={{ color: theme.ink }}>
        {headline(value, column?.format ?? null, config.compact !== false)}
      </p>
      {data.comparison && (
        <p className="mt-1 text-xs" style={{ color: theme.inkSecondary }}>
          {change === null ? (
            "No earlier value to compare with"
          ) : (
            <>
              {/* Direction is carried by the arrow and the sign, not by colour alone. */}
              <span className="font-medium" style={{ color: good === null ? theme.inkSecondary : good ? theme.good : theme.bad }}>
                {change > 0 ? "▲ +" : change < 0 ? "▼ " : ""}
                {new Intl.NumberFormat("en-AU", { style: "percent", maximumFractionDigits: 1 }).format(change)}
              </span>{" "}
              vs {COMPARED_WITH[data.comparison.mode] ?? "before"} ({formatMeasure(previous, column?.format)})
            </>
          )}
        </p>
      )}
    </div>
  );
}
