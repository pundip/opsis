// Chart colours and chrome for light and dark mode.
//
// The default categorical palette passes the colour-blind separation, lightness
// and chroma checks in both modes, in this order. The order is part of what makes
// it safe, so series take colours in sequence and never skip or cycle.
import { useSyncExternalStore } from "react";

export interface ChartTheme {
  mode: "light" | "dark";
  palette: string[];
  other: string; // the "Other" bucket and de-emphasised marks
  surface: string;
  ink: string;
  inkSecondary: string;
  muted: string;
  grid: string;
  axis: string;
  good: string;
  bad: string;
}

const LIGHT: ChartTheme = {
  mode: "light",
  palette: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  other: "#898781",
  surface: "#ffffff",
  ink: "#0b0b0b",
  inkSecondary: "#52514e",
  muted: "#898781",
  grid: "#e1e0d9",
  axis: "#c3c2b7",
  good: "#006300",
  bad: "#b42318",
};

const DARK: ChartTheme = {
  mode: "dark",
  palette: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"],
  other: "#898781",
  surface: "#18181b",
  ink: "#f0efec",
  inkSecondary: "#c3c2b7",
  muted: "#898781",
  grid: "#2c2c2a",
  axis: "#383835",
  good: "#0ca30c",
  bad: "#e66767",
};

export interface ThemeSpec {
  preset?: string;
  mode?: "auto" | "light" | "dark";
  palette?: string[];
}

const query = typeof window !== "undefined" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
const subscribe = (callback: () => void) => {
  query?.addEventListener("change", callback);
  return () => query?.removeEventListener("change", callback);
};

export function resolveTheme(spec: ThemeSpec | undefined, systemDark: boolean): ChartTheme {
  const mode = spec?.mode && spec.mode !== "auto" ? spec.mode : systemDark ? "dark" : "light";
  const base = mode === "dark" ? DARK : LIGHT;
  return spec?.palette?.length ? { ...base, palette: spec.palette } : base;
}

export function useChartTheme(spec: ThemeSpec | undefined): ChartTheme {
  const systemDark = useSyncExternalStore(subscribe, () => query?.matches ?? false, () => false);
  return resolveTheme(spec, systemDark);
}

/** The colour for the n-th series. Past the palette, everything is "Other". */
export function seriesColor(theme: ChartTheme, index: number): string {
  return theme.palette[index] ?? theme.other;
}
