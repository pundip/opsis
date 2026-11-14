import { Cartesian } from "../Cartesian";
import type { RenderProps } from "../types";

export default function Render(props: RenderProps) {
  return <Cartesian kind="combo" {...props} />;
}
