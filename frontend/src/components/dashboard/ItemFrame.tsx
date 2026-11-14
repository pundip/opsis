// One dashboard tile: fetches the item's data and renders its visual.
import { Suspense, useState } from "react";
import { ApiError } from "../../lib/api";
import type { AppConfig } from "../../lib/config";
import {
  type DataSource,
  type Item,
  type Spec,
  type ViewState,
  bindingGrain,
  useItemData,
} from "../../lib/dashboard";
import { GRAIN_LABELS, type Grain } from "../../lib/semantic";
import type { ChartTheme } from "../../lib/theme";
import { manifests, renderers } from "../../visuals/registry";
import { Badge } from "../ui";

const CALENDAR_LADDER: Grain[] = ["year", "quarter", "month", "week", "day"];
const FISCAL_LADDER: Grain[] = ["fiscal_year", "fiscal_quarter", "fiscal_month", "week", "day"];
const CHARTS = new Set(["bar", "line", "area", "combo", "pie", "scatter"]);

interface Props {
  dashboardId: string;
  item: Item;
  spec: Spec;
  source: DataSource;
  state: ViewState;
  onState: (next: ViewState) => void;
  theme: ChartTheme;
  calendar: AppConfig["calendar"];
  editing?: boolean;
}

function Message({ children, tone = "muted" }: { children: React.ReactNode; tone?: "muted" | "error" }) {
  return (
    <div
      role={tone === "error" ? "alert" : undefined}
      className={`grid h-full place-items-center p-2 text-center text-xs ${tone === "error" ? "text-red-700 dark:text-red-400" : "text-zinc-500"}`}
    >
      <div>{children}</div>
    </div>
  );
}

export function ItemFrame({ dashboardId, item, spec, source, state, onState, theme, calendar, editing }: Props) {
  const manifest = manifests[item.visual];
  const [asTable, setAsTable] = useState(false);
  const data = useItemData(dashboardId, item, spec, source, state);
  const Renderer = renderers[asTable ? "table" : item.visual];
  const config = item.config ?? {};

  const grainBinding = Object.values(item.bindings ?? {}).flat().find((b) => bindingGrain(b));
  const baseGrain = grainBinding ? bindingGrain(grainBinding) : undefined;
  const grain = state.grains[item.id] ?? baseGrain;
  const ladder = baseGrain?.startsWith("fiscal") ? FISCAL_LADDER : CALENDAR_LADDER;
  const canDrill = Boolean(baseGrain && manifest?.capabilities?.drillDown);

  const own = state.cross.find((c) => c.source === item.id);
  const crossEnabled = Boolean(spec.interactions?.crossFilter && manifest?.capabilities?.crossFilter);
  const select = (field: string, value: unknown, label: string) => {
    const rest = state.cross.filter((c) => c.source !== item.id);
    const same = own && own.field === field && own.values[0] === value;
    onState({
      ...state,
      cross: same ? rest : [...rest, { field, values: [value as string | number | boolean], source: item.id, label }],
    });
  };

  const measureTitle = data.data?.columns
    ?.filter((c) => c.role === "measure")
    .map((c) => c.label ?? c.name)
    .join(", ");
  const title = item.title ?? (item.visual === "text" ? "" : measureTitle || manifest?.name || item.visual);

  let body: React.ReactNode;
  if (!manifest || !Renderer) {
    body = <Message tone="error">There is no visual called "{item.visual}".</Message>;
  } else if (data.isError) {
    const details = data.error instanceof ApiError ? data.error.details : [];
    body = (
      <Message tone="error">
        {data.error.message}
        {details.map((d) => (
          <span key={d.message} className="mt-1 block">
            {d.message}
          </span>
        ))}
      </Message>
    );
  } else if (!data.data) {
    body = <Message>Loading...</Message>;
  } else if (data.data.empty && item.visual !== "text") {
    body = <Message>{editing ? "Choose fields for this visual in the panel on the right." : "This visual has no fields yet."}</Message>;
  } else if (!data.data.empty && data.data.rows.length === 0) {
    body = <Message>No data for the current filters.</Message>;
  } else {
    body = (
      <Suspense fallback={<Message>Loading...</Message>}>
        <Renderer
          data={data.data}
          item={item}
          config={config}
          theme={theme}
          calendar={calendar}
          onSelect={crossEnabled ? select : undefined}
          selected={own}
        />
      </Suspense>
    );
  }

  return (
    <div className="flex h-full flex-col p-3" style={{ background: theme.surface, color: theme.ink }}>
      {(title || canDrill || data.data?.rlsBypassed) && (
        <div className="mb-1.5 flex min-h-5 items-center gap-2">
          <h3 className="mr-auto truncate text-sm font-medium" style={{ color: theme.inkSecondary }} title={title}>
            {title}
          </h3>
          {data.data?.rlsBypassed && (
            <span title="Row-level security rules exist on this data but do not apply to your role, so you are seeing every row.">
              <Badge tone="bad">RLS bypassed</Badge>
            </span>
          )}
          {data.isFetching && data.data && <span className="text-xs text-zinc-500">Updating</span>}
          {canDrill && grain && (
            <select
              aria-label="Group dates by"
              value={grain}
              onChange={(event) => onState({ ...state, grains: { ...state.grains, [item.id]: event.target.value as Grain } })}
              className="rgl-no-drag rounded border bg-transparent px-1 py-0.5 text-xs"
              style={{ borderColor: theme.axis, color: theme.inkSecondary }}
            >
              {ladder.map((option) => (
                <option key={option} value={option}>
                  {GRAIN_LABELS[option]}
                </option>
              ))}
            </select>
          )}
          {CHARTS.has(item.visual) && data.data && !data.data.empty && (
            <button
              type="button"
              onClick={() => setAsTable(!asTable)}
              className="rgl-no-drag rounded border px-1.5 py-0.5 text-xs"
              style={{ borderColor: theme.axis, color: theme.inkSecondary }}
              title="Switch between the chart and a table of the same data"
            >
              {asTable ? "Chart" : "Table"}
            </button>
          )}
        </div>
      )}
      <div className="rgl-no-drag min-h-0 flex-1">{body}</div>
    </div>
  );
}
