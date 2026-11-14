import type { AppConfig } from "../lib/config";
import type { CrossFilter, Item, ItemData } from "../lib/dashboard";
import type { ChartTheme } from "../lib/theme";

/** What every visual's Render component receives. */
export interface RenderProps {
  data: ItemData;
  item: Item;
  config: Record<string, unknown>;
  theme: ChartTheme;
  calendar: AppConfig["calendar"];
  /** Call to cross-filter the rest of the dashboard on a clicked value. Absent when disabled. */
  onSelect?: (field: string, value: unknown, label: string) => void;
  /** The cross-filter this item currently applies, if any. */
  selected?: CrossFilter;
}
