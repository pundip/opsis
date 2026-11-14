// Pure functions that turn an item's query result into ECharts options.
//
// Mark rules followed here: bars at most 24px thick with a rounded data end,
// 2px lines, light area washes, hairline solid gridlines, one value axis (never
// two), a legend whenever there are two or more series, colours taken from the
// palette in order with the overflow folded into "Other", and text in ink
// colours, never in the series colour.
import type { EChartsCoreOption } from "echarts/core";
import { periodLabel } from "./calendar";
import type { AppConfig } from "./config";
import { type Item, type ItemData, bound } from "./dashboard";
import { formatMeasure } from "./format";
import type { ResultColumn } from "./semantic";
import { type ChartTheme, seriesColor } from "./theme";

type Calendar = AppConfig["calendar"];
type Config = Record<string, unknown>;

export interface ChartInput {
  data: ItemData;
  item: Item;
  config: Config;
  theme: ChartTheme;
  calendar: Calendar;
}

export interface ChartResult {
  option: EChartsCoreOption;
  /** For click handling: the field on the category axis and its raw values, in axis order. */
  categoryField?: string;
  categories?: unknown[];
  selectable?: boolean;
}

export const OTHER = "Other";
const BLANK = "(blank)";

export function displayValue(value: unknown, column: ResultColumn | undefined, calendar: Calendar): string {
  if (value === null || value === undefined || value === "") return BLANK;
  if (column?.role === "time" && column.grain) return periodLabel(String(value), column.grain, calendar);
  if (column?.type === "timestamp") return String(value).replace("T", " ");
  return String(value);
}

export function compactNumber(value: number): string {
  return new Intl.NumberFormat("en-AU", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

const number = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : null);

function chrome(theme: ChartTheme) {
  const axisLabel = { color: theme.muted, fontSize: 11 };
  return {
    text: { fontFamily: "ui-sans-serif, system-ui, 'Segoe UI', sans-serif" },
    tooltip: {
      backgroundColor: theme.surface,
      borderColor: theme.grid,
      borderWidth: 1,
      textStyle: { color: theme.ink, fontSize: 12 },
      confine: true,
    },
    legend: {
      type: "scroll" as const,
      top: 0,
      left: 0,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: theme.inkSecondary, fontSize: 12 },
      pageTextStyle: { color: theme.muted },
    },
    categoryAxis: {
      type: "category" as const,
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { ...axisLabel, hideOverlap: true, width: 110, overflow: "truncate" as const },
    },
    valueAxis: {
      type: "value" as const,
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: theme.grid, width: 1, type: "solid" as const } },
      axisLabel: { ...axisLabel, formatter: (v: number) => (Math.abs(v) >= 10_000 ? compactNumber(v) : String(v)) },
    },
  };
}

interface SeriesData {
  name: string;
  values: (number | null)[];
  column: ResultColumn;
  kind: "bar" | "line";
  color: string;
}

/** Bar, line, area and combo charts: one category axis, one value axis. */
export function cartesianOption(kind: "bar" | "line" | "area" | "combo", input: ChartInput): ChartResult {
  const { data, item, config, theme, calendar } = input;
  const index = (ref: string | undefined) => (ref ? data.columns.findIndex((c) => c.name === ref) : -1);
  const categoryRef = bound(item, kind === "bar" || kind === "combo" ? "category" : "x")[0];
  const seriesRef = kind === "combo" ? undefined : bound(item, "series")[0];
  const measures: { ref: string; kind: "bar" | "line" }[] =
    kind === "combo"
      ? [
          ...bound(item, "bars").map((ref) => ({ ref, kind: "bar" as const })),
          ...bound(item, "lines").map((ref) => ({ ref, kind: "line" as const })),
        ]
      : bound(item, kind === "bar" ? "value" : "y").map((ref) => ({ ref, kind: kind === "bar" ? ("bar" as const) : ("line" as const) }));
  const ci = index(categoryRef);
  const usable = measures.filter((m) => index(m.ref) >= 0);
  if (ci < 0 || usable.length === 0) return { option: {} };
  const categoryColumn = data.columns[ci]!;

  // Categories in the order the query returned them.
  const categories: unknown[] = [];
  const position = new Map<string, number>();
  for (const row of data.rows) {
    const key = JSON.stringify(row[ci]);
    if (!position.has(key)) {
      position.set(key, categories.length);
      categories.push(row[ci]);
    }
  }
  const at = (row: unknown[]) => position.get(JSON.stringify(row[ci]))!;

  const series: SeriesData[] = [];
  const si = index(seriesRef);
  if (si >= 0) {
    // One measure, split by a dimension. Keep the largest groups, fold the rest into Other.
    const measure = usable[0]!;
    const mi = index(measure.ref);
    const totals = new Map<string, number>();
    for (const row of data.rows) {
      const name = displayValue(row[si], data.columns[si], calendar);
      totals.set(name, (totals.get(name) ?? 0) + Math.abs(number(row[mi]) ?? 0));
    }
    const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name);
    const kept = ranked.length > theme.palette.length ? ranked.slice(0, theme.palette.length - 1) : ranked;
    // Colours follow the name, not the rank, so a filter does not repaint the survivors.
    const names = [...kept].sort((a, b) => a.localeCompare(b));
    const lookup = new Map<string, SeriesData>();
    names.forEach((name, i) => {
      lookup.set(name, { name, values: categories.map(() => null), column: data.columns[mi]!, kind: measure.kind, color: seriesColor(theme, i) });
    });
    if (kept.length < ranked.length) {
      lookup.set(OTHER, { name: OTHER, values: categories.map(() => null), column: data.columns[mi]!, kind: measure.kind, color: theme.other });
    }
    for (const row of data.rows) {
      const name = displayValue(row[si], data.columns[si], calendar);
      const target = lookup.get(name) ?? lookup.get(OTHER)!;
      const value = number(row[mi]);
      if (value !== null) target.values[at(row)] = (target.values[at(row)] ?? 0) + value;
    }
    series.push(...lookup.values());
  } else {
    usable.forEach((measure, i) => {
      const mi = index(measure.ref);
      const column = data.columns[mi]!;
      const values: (number | null)[] = categories.map(() => null);
      for (const row of data.rows) values[at(row)] = number(row[mi]);
      series.push({ name: column.label ?? column.name, values, column, kind: measure.kind, color: seriesColor(theme, i) });
    });
  }

  const c = chrome(theme);
  const horizontal = kind === "bar" && config.orientation === "horizontal";
  const stacked = Boolean(config.stacked) && (kind !== "line");
  const showLegend = series.length > 1;
  const showLabels = Boolean(config.showLabels) && categories.length <= 12 && series.length <= 2;
  const labels = categories.map((value) => displayValue(value, categoryColumn, calendar));
  const categoryAxis = { ...c.categoryAxis, data: labels, inverse: horizontal };
  const option: EChartsCoreOption = {
    textStyle: c.text,
    animationDuration: 250,
    aria: { enabled: true },
    grid: { left: 8, right: 16, top: showLegend ? 34 : 12, bottom: 4, containLabel: true },
    tooltip: { ...c.tooltip, trigger: "axis", axisPointer: { type: kind === "bar" ? "shadow" : "line", lineStyle: { color: theme.axis } } },
    legend: showLegend ? c.legend : undefined,
    xAxis: horizontal ? c.valueAxis : categoryAxis,
    yAxis: horizontal ? categoryAxis : c.valueAxis,
    series: series.map((s) => {
      const isArea = kind === "area";
      const base = {
        name: s.name,
        data: s.values,
        tooltip: { valueFormatter: (v: unknown) => formatMeasure(v, s.column.format) },
        emphasis: { focus: "series" as const },
      };
      if (s.kind === "bar") {
        return {
          ...base,
          type: "bar",
          stack: stacked ? "total" : undefined,
          barMaxWidth: 24,
          barGap: "10%",
          itemStyle: {
            color: s.color,
            borderRadius: stacked ? 0 : horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0],
            borderColor: theme.surface,
            borderWidth: stacked ? 1 : 0, // the gap between stacked segments
          },
          label: showLabels
            ? { show: true, position: horizontal ? "right" : "top", color: theme.inkSecondary, fontSize: 11, formatter: (p: { value: unknown }) => formatMeasure(p.value, s.column.format) }
            : undefined,
        };
      }
      return {
        ...base,
        type: "line",
        stack: isArea && stacked ? "total" : undefined,
        smooth: Boolean(config.smooth),
        symbol: "circle",
        symbolSize: 8,
        showSymbol: Boolean(config.showMarkers) || categories.length === 1,
        lineStyle: { width: 2, color: s.color },
        itemStyle: { color: s.color, borderColor: theme.surface, borderWidth: 2 },
        areaStyle: isArea ? { color: s.color, opacity: stacked ? 0.22 : 0.1 } : undefined,
        connectNulls: false,
      };
    }),
  };
  return { option, categoryField: categoryRef, categories, selectable: categoryColumn.role === "dimension" };
}

/** Pie and donut: the largest slices, with the rest folded into Other. */
export function pieOption(input: ChartInput): ChartResult {
  const { data, item, config, theme, calendar } = input;
  const categoryRef = bound(item, "category")[0];
  const ci = data.columns.findIndex((c) => c.name === categoryRef);
  const vi = data.columns.findIndex((c) => c.name === bound(item, "value")[0]);
  if (ci < 0 || vi < 0) return { option: {} };
  const column = data.columns[vi]!;
  const max = Math.min(Number(config.maxSlices ?? 6), theme.palette.length);
  const all = data.rows
    .map((row) => ({ raw: row[ci], name: displayValue(row[ci], data.columns[ci], calendar), value: number(row[vi]) ?? 0 }))
    .filter((slice) => slice.value > 0)
    .sort((a, b) => b.value - a.value);
  const kept = all.length > max ? all.slice(0, max - 1) : all;
  const rest = all.slice(kept.length).reduce((sum, slice) => sum + slice.value, 0);
  const order = [...kept].map((s) => s.name).sort((a, b) => a.localeCompare(b));
  const slices = kept.map((slice) => ({
    name: slice.name,
    value: slice.value,
    itemStyle: { color: seriesColor(theme, order.indexOf(slice.name)) },
  }));
  if (rest > 0) slices.push({ name: OTHER, value: rest, itemStyle: { color: theme.other } });
  const c = chrome(theme);
  return {
    option: {
      textStyle: c.text,
      animationDuration: 250,
      aria: { enabled: true },
      tooltip: { ...c.tooltip, trigger: "item", valueFormatter: (v: unknown) => formatMeasure(v, column.format) },
      series: [
        {
          type: "pie",
          radius: config.donut === false ? "70%" : ["48%", "70%"],
          center: ["50%", "52%"],
          data: slices,
          itemStyle: { borderColor: theme.surface, borderWidth: 2 },
          label: { color: theme.inkSecondary, fontSize: 12, formatter: "{b}\n{d}%" },
          labelLine: { lineStyle: { color: theme.axis } },
          emphasis: { scale: true, scaleSize: 4 },
        },
      ],
    },
    categoryField: categoryRef,
    categories: [...kept.map((s) => s.raw), ...(rest > 0 ? [undefined] : [])],
    selectable: true,
  };
}

/** Scatter: one point per item. Colour keeps the three largest groups plus Other. */
export function scatterOption(input: ChartInput): ChartResult {
  const { data, item, config, theme, calendar } = input;
  const find = (role: string) => data.columns.findIndex((c) => c.name === bound(item, role)[0]);
  const [di, xi, yi, si] = [find("detail"), find("x"), find("y"), find("series")];
  if (di < 0 || xi < 0 || yi < 0) return { option: {} };
  const [xColumn, yColumn] = [data.columns[xi]!, data.columns[yi]!];
  const groups = new Map<string, [number, number, string][]>();
  for (const row of data.rows) {
    const x = number(row[xi]);
    const y = number(row[yi]);
    if (x === null || y === null) continue;
    const group = si >= 0 ? displayValue(row[si], data.columns[si], calendar) : (data.columns[di]!.label ?? "Items");
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group)!.push([x, y, displayValue(row[di], data.columns[di], calendar)]);
  }
  // Every pair of colours is compared in a scatter, so only three are safe.
  const ranked = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  const kept = ranked.length > 3 ? ranked.slice(0, 3) : ranked;
  const other = ranked.slice(kept.length).flatMap(([, points]) => points);
  const names = kept.map(([name]) => name).sort((a, b) => a.localeCompare(b));
  const size = Number(config.pointSize ?? 8);
  const c = chrome(theme);
  const axis = (column: ResultColumn) => ({
    ...c.valueAxis,
    name: column.label ?? column.name,
    nameLocation: "middle" as const,
    nameGap: 28,
    nameTextStyle: { color: theme.inkSecondary, fontSize: 12 },
    scale: true,
  });
  const make = (name: string, points: [number, number, string][], color: string) => ({
    name,
    type: "scatter",
    data: points,
    symbolSize: size,
    itemStyle: { color, opacity: 0.85, borderColor: theme.surface, borderWidth: 1 },
  });
  const series = kept.map(([name, points]) => make(name, points, seriesColor(theme, names.indexOf(name))));
  if (other.length) series.push(make(OTHER, other, theme.other));
  return {
    option: {
      textStyle: c.text,
      animationDuration: 250,
      aria: { enabled: true },
      grid: { left: 8, right: 16, top: series.length > 1 ? 34 : 12, bottom: 26, containLabel: true },
      legend: series.length > 1 ? c.legend : undefined,
      tooltip: {
        ...c.tooltip,
        trigger: "item",
        formatter: (p: { value: [number, number, string]; seriesName: string }) =>
          `<b>${escapeHtml(p.value[2])}</b><br/>${escapeHtml(xColumn.label ?? "")}: ${escapeHtml(formatMeasure(p.value[0], xColumn.format))}` +
          `<br/>${escapeHtml(yColumn.label ?? "")}: ${escapeHtml(formatMeasure(p.value[1], yColumn.format))}`,
      },
      xAxis: axis(xColumn),
      yAxis: { ...axis(yColumn), nameGap: 44 },
      series,
    },
  };
}
