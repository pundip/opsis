// The dashboard grid. Read-only when viewing; draggable and resizable when editing.
import ReactGridLayout, { type Layout, useContainerWidth } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import type { AppConfig } from "../../lib/config";
import type { DataSource, Item, Position, Spec, ViewState } from "../../lib/dashboard";
import type { ChartTheme } from "../../lib/theme";
import { manifests } from "../../visuals/registry";
import { ItemFrame } from "./ItemFrame";

interface Props {
  dashboardId: string;
  spec: Spec;
  source: DataSource;
  state: ViewState;
  onState: (next: ViewState) => void;
  theme: ChartTheme;
  calendar: AppConfig["calendar"];
  editing?: {
    selected: string | null;
    onSelect: (id: string | null) => void;
    onLayout: (positions: Record<string, Position>) => void;
  };
}

export function Canvas({ dashboardId, spec, source, state, onState, theme, calendar, editing }: Props) {
  const { width, containerRef, mounted } = useContainerWidth();
  const layout: Layout = spec.items.map((item: Item) => {
    const min = manifests[item.visual]?.minSize;
    return { i: item.id, ...item.position, minW: min?.w ?? 1, minH: min?.h ?? 1 };
  });

  const changed = (next: Layout) => {
    if (!editing) return;
    const positions: Record<string, Position> = {};
    let moved = false;
    for (const entry of next) {
      const before = spec.items.find((item) => item.id === entry.i)?.position;
      if (!before) continue;
      positions[entry.i] = { x: entry.x, y: entry.y, w: entry.w, h: entry.h };
      if (before.x !== entry.x || before.y !== entry.y || before.w !== entry.w || before.h !== entry.h) moved = true;
    }
    if (moved) editing.onLayout(positions);
  };

  return (
    <div ref={containerRef} className={editing ? "opsis-canvas-editing" : undefined}>
      {mounted && (
        <ReactGridLayout
          width={width}
          layout={layout}
          gridConfig={{ cols: spec.layout.columns, rowHeight: spec.layout.rowHeight, margin: [12, 12], containerPadding: [0, 0] }}
          dragConfig={{ enabled: Boolean(editing), cancel: ".rgl-no-drag" }}
          resizeConfig={{ enabled: Boolean(editing) }}
          // Only a finished drag or resize is an edit. The grid also reports
          // its own compaction (after a removal or an undo), and recording
          // that would bury the real step in the undo history.
          onDragStop={changed}
          onResizeStop={changed}
        >
          {spec.items.map((item) => {
            const selected = editing?.selected === item.id;
            return (
              <div
                key={item.id}
                data-item={item.id}
                className={`overflow-hidden rounded-lg border ${selected ? "border-brand ring-2 ring-brand/40" : "border-zinc-200 dark:border-zinc-800"} ${editing ? "cursor-move" : ""}`}
              >
                {/* The grid library owns the outer element's mouse handlers, so
                    selection lives on this inner one. Enter or Space selects too. */}
                <div
                  className="h-full"
                  role={editing ? "button" : undefined}
                  aria-pressed={editing ? selected : undefined}
                  aria-label={editing ? `Select visual ${item.title ?? item.id}` : undefined}
                  tabIndex={editing ? 0 : undefined}
                  onClickCapture={editing ? () => editing.onSelect(item.id) : undefined}
                  onKeyDown={
                    editing
                      ? (event) => {
                          if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
                            event.preventDefault();
                            editing.onSelect(item.id);
                          }
                        }
                      : undefined
                  }
                >
                <ItemFrame
                  dashboardId={dashboardId}
                  item={item}
                  spec={spec}
                  source={source}
                  state={state}
                  onState={onState}
                  theme={theme}
                  calendar={calendar}
                  editing={Boolean(editing)}
                />
                </div>
              </div>
            );
          })}
        </ReactGridLayout>
      )}
    </div>
  );
}
