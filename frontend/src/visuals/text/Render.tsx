import { Markdown } from "../../lib/markdown";
import type { RenderProps } from "../types";

export default function Render({ config, theme }: RenderProps) {
  return (
    <div className="h-full overflow-auto" style={{ color: theme.ink }}>
      <Markdown source={typeof config.markdown === "string" ? config.markdown : ""} />
    </div>
  );
}
