// Shared renderer for the bar, line, area and combo visuals.
import { useMemo } from "react";
import { EChart } from "../components/EChart";
import { cartesianOption, displayValue } from "../lib/chartOptions";
import type { RenderProps } from "./types";

export function Cartesian({ kind, ...props }: RenderProps & { kind: "bar" | "line" | "area" | "combo" }) {
  const { data, item, config, theme, calendar, onSelect } = props;
  const result = useMemo(
    () => cartesianOption(kind, { data, item, config, theme, calendar }),
    [kind, data, item, config, theme, calendar],
  );
  const column = data.columns.find((c) => c.name === result.categoryField);
  const canSelect = onSelect && result.selectable && result.categoryField;
  return (
    <EChart
      option={result.option}
      label={`${item.title ?? kind} chart`}
      onClick={
        canSelect
          ? (event) => {
              const value = result.categories?.[event.dataIndex];
              if (value !== undefined && value !== null) {
                onSelect(result.categoryField!, value, displayValue(value, column, calendar));
              }
            }
          : undefined
      }
    />
  );
}
