import { describe, expect, it } from "vitest";
import { cartesianOption, displayValue, OTHER, pieOption, scatterOption } from "./chartOptions";
import { type Item, type ItemData, type Manifest, defaultConfig, newId } from "./dashboard";
import { countChanges, diffLines, withContext } from "./diff";
import { push, redo, start, undo } from "./history";
import { parseBlocks, safeHref } from "./markdown";
import type { ResultColumn } from "./semantic";
import { resolveTheme, seriesColor } from "./theme";

const calendar = { timezone: "Australia/Melbourne", weekStart: "monday" as const, fiscalYearStartMonth: 7, fiscalYearLabel: "fy_end_year" as const };
const theme = resolveTheme(undefined, false);
const column = (name: string, role: ResultColumn["role"], extra: Partial<ResultColumn> = {}): ResultColumn => ({
  name, label: name.split(".")[1]!, type: role === "measure" ? "float" : "string", role, format: null, grain: null, ...extra,
});
const data = (columns: ResultColumn[], rows: unknown[][]): ItemData => ({
  itemId: "v", columns, rows, rowCount: rows.length, truncated: false, rlsBypassed: false,
});
const item = (visual: string, bindings: Item["bindings"]): Item => ({
  id: "v", visual, position: { x: 0, y: 0, w: 6, h: 6 }, dataset: "g", bindings,
});
type Series = { name: string; data: (number | null)[]; type: string; stack?: string; itemStyle: { color: string }; barMaxWidth?: number };
const seriesOf = (option: object) => (option as { series: Series[] }).series;

describe("history", () => {
  it("undoes and redoes, and a new change clears redo", () => {
    let h = push(push(start("a"), "b"), "c");
    expect(h.present).toBe("c");
    h = undo(h);
    expect([h.present, h.future]).toEqual(["b", ["c"]]);
    h = redo(h);
    expect(h.present).toBe("c");
    h = push(undo(undo(h)), "x");
    expect([h.past, h.present, h.future]).toEqual([["a"], "x", []]);
    expect(undo(start("a")).present).toBe("a");
    expect(redo(start("a")).present).toBe("a");
    expect(push(h, "x")).toBe(h); // an identical state is not recorded
  });
});

describe("diffLines", () => {
  it("finds added and removed lines", () => {
    const lines = diffLines("a\nb\nc\nd", "a\nB\nc\nd\ne");
    expect(lines.filter((l) => l.kind !== "same")).toEqual([
      { kind: "removed", text: "b" },
      { kind: "added", text: "B" },
      { kind: "added", text: "e" },
    ]);
    expect(countChanges(lines)).toEqual({ added: 2, removed: 1 });
    expect(countChanges(diffLines("same", "same"))).toEqual({ added: 0, removed: 0 });
  });

  it("keeps only the lines around a change", () => {
    const before = Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n");
    const after = before.replace("line 10", "changed");
    const shown = withContext(diffLines(before, after), 1);
    expect(shown.map((l) => (l === "gap" ? "gap" : l.text))).toEqual(["line 9", "line 10", "changed", "line 11"]);
  });
});

describe("cartesianOption", () => {
  const rows = [["Library", 30, 3], ["Sport", 10, 1], [null, 5, 2]];
  const columns = [column("g.fund", "dimension"), column("g.total", "measure", { format: "currency_aud" }), column("g.count", "measure")];

  it("makes one series per measure, in query order, with thin rounded bars", () => {
    const result = cartesianOption("bar", { data: data(columns, rows), item: item("bar", { category: ["g.fund"], value: ["g.total", "g.count"] }), config: {}, theme, calendar });
    const series = seriesOf(result.option);
    expect(series.map((s) => [s.name, s.data, s.type])).toEqual([["total", [30, 10, 5], "bar"], ["count", [3, 1, 2], "bar"]]);
    expect(series[0]!.barMaxWidth).toBe(24);
    expect(series.map((s) => s.itemStyle.color)).toEqual([theme.palette[0], theme.palette[1]]);
    expect((result.option as { xAxis: { data: string[] } }).xAxis.data).toEqual(["Library", "Sport", "(blank)"]);
    expect((result.option as { legend?: object }).legend).toBeDefined(); // two series: legend
    expect(result.categories).toEqual(["Library", "Sport", null]);
    expect(result.selectable).toBe(true);
  });

  it("pivots a split-by dimension, colours by name, and has no legend for one series", () => {
    const split = [["2026-07-01", "VIC", 5], ["2026-07-01", "NSW", 7], ["2026-10-01", "VIC", 9]];
    const cols = [column("g.day", "time", { grain: "fiscal_quarter" }), column("g.region", "dimension"), column("g.total", "measure")];
    const result = cartesianOption("line", { data: data(cols, split), item: item("line", { x: [{ field: "g.day", grain: "fiscal_quarter" }], y: ["g.total"], series: ["g.region"] }), config: {}, theme, calendar });
    const series = seriesOf(result.option);
    expect(series.map((s) => [s.name, s.data])).toEqual([["NSW", [7, null]], ["VIC", [5, 9]]]);
    expect((result.option as { xAxis: { data: string[] } }).xAxis.data).toEqual(["Q1 FY2027", "Q2 FY2027"]);
    expect(result.selectable).toBe(false); // a time axis does not cross-filter
    const single = cartesianOption("line", { data: data([cols[0]!, cols[2]!], [["2026-07-01", 5]]), item: item("line", { x: ["g.day"], y: ["g.total"] }), config: {}, theme, calendar });
    expect((single.option as { legend?: object }).legend).toBeUndefined();
  });

  it("folds series beyond the palette into Other and never invents colours", () => {
    const many = Array.from({ length: 12 }, (_, i) => ["A", `s${String(i).padStart(2, "0")}`, 100 - i]);
    const cols = [column("g.fund", "dimension"), column("g.region", "dimension"), column("g.total", "measure")];
    const series = seriesOf(cartesianOption("bar", { data: data(cols, many), item: item("bar", { category: ["g.fund"], value: ["g.total"], series: ["g.region"] }), config: { stacked: true }, theme, calendar }).option);
    expect(series).toHaveLength(8);
    expect(series.at(-1)!.name).toBe(OTHER);
    expect(series.at(-1)!.data).toEqual([93 + 92 + 91 + 90 + 89]);
    expect(series.at(-1)!.itemStyle.color).toBe(theme.other);
    expect(new Set(series.map((s) => s.itemStyle.color)).size).toBe(8);
    expect(series.every((s) => s.stack === "total")).toBe(true);
  });

  it("puts combo bars and lines on one shared value axis by default", () => {
    const result = cartesianOption("combo", { data: data(columns, rows), item: item("combo", { category: ["g.fund"], bars: ["g.total"], lines: ["g.count"] }), config: {}, theme, calendar });
    expect(seriesOf(result.option).map((s) => s.type)).toEqual(["bar", "line"]);
    expect(Array.isArray((result.option as { yAxis: unknown }).yAxis)).toBe(false);
  });

  it("gives a combo measure its own right-hand axis when asked", () => {
    const result = cartesianOption("combo", { data: data(columns, rows), item: item("combo", { category: ["g.fund"], bars: ["g.total"], lines: [{ field: "g.count", axis: "right" }] }), config: {}, theme, calendar });
    const series = seriesOf(result.option) as (ReturnType<typeof seriesOf>[number] & { yAxisIndex: number })[];
    expect(series.map((s) => [s.name, s.type, s.yAxisIndex])).toEqual([["total", "bar", 0], ["count (right axis)", "line", 1]]);
    const axes = (result.option as { yAxis: { name: string; position: string; splitLine: { show?: boolean } }[] }).yAxis;
    expect(axes.map((a) => [a.name, a.position])).toEqual([["total", "left"], ["count", "right"]]);
    expect(axes[1]!.splitLine.show).toBe(false); // one set of grid lines
    // Every measure on the right is the same as every measure on the left: one axis.
    const all = cartesianOption("combo", { data: data(columns, rows), item: item("combo", { category: ["g.fund"], bars: [{ field: "g.total", axis: "right" }], lines: [{ field: "g.count", axis: "right" }] }), config: {}, theme, calendar });
    expect(Array.isArray((all.option as { yAxis: unknown }).yAxis)).toBe(false);
  });

  it("swaps the axes for horizontal bars and returns nothing without its fields", () => {
    const horizontal = cartesianOption("bar", { data: data(columns, rows), item: item("bar", { category: ["g.fund"], value: ["g.total"] }), config: { orientation: "horizontal" }, theme, calendar });
    expect((horizontal.option as { yAxis: { type: string; inverse: boolean } }).yAxis).toMatchObject({ type: "category", inverse: true });
    expect(cartesianOption("bar", { data: data(columns, rows), item: item("bar", {}), config: {}, theme, calendar }).option).toEqual({});
  });
});

describe("pieOption and scatterOption", () => {
  it("keeps the largest slices and sums the rest", () => {
    const rows = [["a", 5], ["b", 50], ["c", 20], ["d", 1], ["e", 0], ["f", -3]];
    const result = pieOption({ data: data([column("g.fund", "dimension"), column("g.total", "measure")], rows), item: item("pie", { category: ["g.fund"], value: ["g.total"] }), config: { maxSlices: 3 }, theme, calendar });
    const slices = (result.option as { series: { data: { name: string; value: number }[] }[] }).series[0]!.data;
    expect(slices.map((s) => [s.name, s.value])).toEqual([["b", 50], ["c", 20], [OTHER, 6]]);
    expect(result.categories).toEqual(["b", "c", undefined]);
  });

  it("limits scatter colours to three groups plus Other", () => {
    const rows = Array.from({ length: 10 }, (_, i) => [`d${i}`, i, i * 2, `g${i % 5}`]);
    const cols = [column("g.donor", "dimension"), column("g.count", "measure"), column("g.total", "measure"), column("g.region", "dimension")];
    const series = seriesOf(scatterOption({ data: data(cols, rows), item: item("scatter", { detail: ["g.donor"], x: ["g.count"], y: ["g.total"], series: ["g.region"] }), config: {}, theme, calendar }).option);
    expect(series).toHaveLength(4);
    expect(series.at(-1)!.name).toBe(OTHER);
    expect(series.reduce((n, s) => n + s.data.length, 0)).toBe(10);
  });
});

describe("helpers", () => {
  it("formats values for display", () => {
    expect(displayValue(null, undefined, calendar)).toBe("(blank)");
    expect(displayValue("2026-11-01", column("g.d", "time", { grain: "month" }), calendar)).toBe("Nov 2026");
    expect(displayValue("2026-11-01T09:30:00", column("g.d", "dimension", { type: "timestamp" }), calendar)).toBe("2026-11-01 09:30:00");
  });

  it("resolves themes and palette overflow", () => {
    expect(resolveTheme({ mode: "dark" }, false).mode).toBe("dark");
    expect(resolveTheme({ mode: "auto" }, true).mode).toBe("dark");
    expect(resolveTheme({ palette: ["#111111", "#222222"] }, false).palette).toEqual(["#111111", "#222222"]);
    expect(seriesColor(theme, 0)).toBe(theme.palette[0]);
    expect(seriesColor(theme, 99)).toBe(theme.other);
  });

  it("generates ids and default config", () => {
    expect(newId("v", ["v_1", "v_2", "f_1"])).toBe("v_3");
    const manifest = { configSchema: { type: "object", properties: { a: { default: true }, b: {} } } } as unknown as Manifest;
    expect(defaultConfig(manifest)).toEqual({ a: true });
  });
});

describe("markdown", () => {
  it("parses headings, paragraphs and lists", () => {
    expect(parseBlocks("# Title\n\nSome\ntext.\n\n- one\n- two\n1. first\n### Small")).toEqual([
      { type: "heading", level: 1, text: "Title" },
      { type: "paragraph", text: "Some text." },
      { type: "list", ordered: false, items: ["one", "two"] },
      { type: "list", ordered: true, items: ["first"] },
      { type: "heading", level: 3, text: "Small" },
    ]);
  });

  it("only allows safe link targets", () => {
    expect(safeHref("https://example.com/a")).toBe("https://example.com/a");
    expect(safeHref("mailto:a@example.com")).toBe("mailto:a@example.com");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref(" JavaScript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,<script>")).toBeNull();
  });
});
