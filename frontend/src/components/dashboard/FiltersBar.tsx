// The row of filter controls above a dashboard, plus active cross-filter chips.
import { useEffect, useState } from "react";
import {
  type DataSource,
  type FilterControl,
  type Spec,
  type ViewState,
  useFilterOptions,
} from "../../lib/dashboard";
import { RELATIVE_RANGES } from "../../lib/semantic";

const CONTROL = "rounded-md border border-zinc-300 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-950";

interface ControlProps {
  dashboardId: string;
  control: FilterControl;
  source: DataSource;
  value: unknown;
  onChange: (value: unknown) => void;
}

function DateRange({ control, value, onChange }: ControlProps) {
  const range = (value ?? {}) as { relative?: string; from?: string; to?: string };
  const custom = !range.relative && (range.from !== undefined || range.to !== undefined);
  const selected = custom ? "custom" : (range.relative ?? "all_time");
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <select
        id={`filter-${control.id}`}
        className={CONTROL}
        value={selected}
        onChange={(event) => {
          const next = event.target.value;
          if (next === "custom") onChange({ from: "", to: "" });
          else onChange({ relative: next });
        }}
      >
        <option value="all_time">All dates</option>
        {RELATIVE_RANGES.filter(([key]) => key).map(([key, label]) => (
          <option key={key} value={key}>
            {label}
          </option>
        ))}
        <option value="custom">Custom range</option>
      </select>
      {custom && (
        <>
          <input
            type="date"
            aria-label={`${control.label ?? "Date"} from`}
            className={CONTROL}
            value={range.from ?? ""}
            onChange={(e) => onChange({ ...range, from: e.target.value })}
          />
          <input
            type="date"
            aria-label={`${control.label ?? "Date"} to`}
            className={CONTROL}
            value={range.to ?? ""}
            onChange={(e) => onChange({ ...range, to: e.target.value })}
          />
        </>
      )}
    </span>
  );
}

function Choice({ dashboardId, control, source, value, onChange }: ControlProps) {
  const options = useFilterOptions(dashboardId, control, source, true);
  const values = options.data?.values ?? [];
  if (control.type === "select") {
    return (
      <select
        id={`filter-${control.id}`}
        className={CONTROL}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(event) => onChange(values.find((v) => String(v) === event.target.value) ?? null)}
      >
        <option value="">All</option>
        {values.map((option) => (
          <option key={String(option)} value={String(option)}>
            {String(option)}
          </option>
        ))}
      </select>
    );
  }
  const chosen = Array.isArray(value) ? (value as unknown[]) : [];
  const toggle = (option: unknown) =>
    onChange(chosen.includes(option) ? chosen.filter((v) => v !== option) : [...chosen, option]);
  return (
    <details className="relative inline-block">
      <summary id={`filter-${control.id}`} className={`${CONTROL} cursor-pointer list-none`}>
        {chosen.length === 0 ? "All" : chosen.length === 1 ? String(chosen[0]) : `${chosen.length} selected`}
        <span aria-hidden> ▾</span>
      </summary>
      <div className="absolute z-20 mt-1 max-h-64 min-w-40 overflow-auto rounded-md border border-zinc-300 bg-white p-2 shadow-lg dark:border-zinc-700 dark:bg-zinc-900">
        {options.isError && <p className="text-xs text-red-700 dark:text-red-400">{options.error.message}</p>}
        {values.map((option) => (
          <label key={String(option)} className="flex items-center gap-2 py-0.5 text-sm whitespace-nowrap">
            <input type="checkbox" checked={chosen.includes(option)} onChange={() => toggle(option)} />
            {String(option)}
          </label>
        ))}
        {chosen.length > 0 && (
          <button type="button" className="mt-1 text-xs text-brand underline" onClick={() => onChange([])}>
            Clear
          </button>
        )}
      </div>
    </details>
  );
}

function Search({ control, value, onChange }: ControlProps) {
  const [text, setText] = useState(typeof value === "string" ? value : "");
  useEffect(() => setText(typeof value === "string" ? value : ""), [value]);
  return (
    <input
      id={`filter-${control.id}`}
      type="search"
      className={CONTROL}
      placeholder="Contains..."
      value={text}
      onChange={(event) => setText(event.target.value)}
      onBlur={() => onChange(text)}
      onKeyDown={(event) => event.key === "Enter" && onChange(text)}
    />
  );
}

interface Props {
  dashboardId: string;
  spec: Spec;
  source: DataSource;
  state: ViewState;
  onState: (next: ViewState) => void;
}

export function FiltersBar({ dashboardId, spec, source, state, onState }: Props) {
  const controls = spec.filters ?? [];
  if (controls.length === 0 && state.cross.length === 0) return null;
  return (
    <div className="mb-3 flex flex-wrap items-end gap-x-4 gap-y-2" role="group" aria-label="Filters">
      {controls.map((control) => {
        const props: ControlProps = {
          dashboardId,
          control,
          source,
          value: control.id in state.filters ? state.filters[control.id] : control.default,
          onChange: (value) => onState({ ...state, filters: { ...state.filters, [control.id]: value } }),
        };
        return (
          <div key={control.id}>
            <label htmlFor={`filter-${control.id}`} className="mb-0.5 block text-xs text-zinc-600 dark:text-zinc-400">
              {control.label ?? control.field.split(".")[1]?.replace(/_/g, " ")}
            </label>
            {control.type === "dateRange" ? <DateRange {...props} /> : control.type === "search" ? <Search {...props} /> : <Choice {...props} />}
          </div>
        );
      })}
      {state.cross.map((cross) => (
        <button
          key={cross.source}
          type="button"
          className="rounded-full border border-zinc-300 px-2.5 py-1 text-xs dark:border-zinc-700"
          title="Remove this selection"
          onClick={() => onState({ ...state, cross: state.cross.filter((c) => c.source !== cross.source) })}
        >
          {cross.field.split(".")[1]?.replace(/_/g, " ")}: {cross.label} <span aria-hidden>×</span>
        </button>
      ))}
      {(Object.keys(state.filters).length > 0 || state.cross.length > 0) && (
        <button type="button" className="py-1 text-xs text-brand underline" onClick={() => onState({ filters: {}, cross: [], grains: state.grains })}>
          Reset filters
        </button>
      )}
    </div>
  );
}
