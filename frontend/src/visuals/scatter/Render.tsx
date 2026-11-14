import { useMemo } from "react";
import { EChart } from "../../components/EChart";
import { scatterOption } from "../../lib/chartOptions";
import type { RenderProps } from "../types";

export default function Render({ data, item, config, theme, calendar }: RenderProps) {
  const result = useMemo(
    () => scatterOption({ data, item, config, theme, calendar }),
    [data, item, config, theme, calendar],
  );
  return <EChart option={result.option} label={`${item.title ?? "Scatter"} plot`} />;
}
