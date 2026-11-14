import { useMemo } from "react";
import { EChart } from "../../components/EChart";
import { displayValue, pieOption } from "../../lib/chartOptions";
import type { RenderProps } from "../types";

export default function Render({ data, item, config, theme, calendar, onSelect }: RenderProps) {
  const result = useMemo(
    () => pieOption({ data, item, config, theme, calendar }),
    [data, item, config, theme, calendar],
  );
  const column = data.columns.find((c) => c.name === result.categoryField);
  return (
    <EChart
      option={result.option}
      label={`${item.title ?? "Pie"} chart`}
      onClick={
        onSelect && result.categoryField
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
