// A thin React wrapper around ECharts (canvas renderer, only the chart types we use).
import { BarChart, LineChart, PieChart, ScatterChart } from "echarts/charts";
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

echarts.use([
  BarChart, LineChart, PieChart, ScatterChart,
  GridComponent, TooltipComponent, LegendComponent, AriaComponent, CanvasRenderer,
]);

export interface ChartClick {
  dataIndex: number;
  seriesName?: string;
  name?: string;
}

interface Props {
  option: echarts.EChartsCoreOption;
  label: string;
  onClick?: (event: ChartClick) => void;
}

export function EChart({ option, label, onClick }: Props) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const handler = useRef(onClick);
  handler.current = onClick;

  useEffect(() => {
    const instance = echarts.init(element.current!);
    chart.current = instance;
    instance.on("click", (event) => handler.current?.(event as ChartClick));
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(element.current!);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, true);
  }, [option]);

  return (
    <div
      ref={element}
      role="img"
      aria-label={label}
      className={`h-full w-full ${onClick ? "cursor-pointer" : ""}`}
    />
  );
}
