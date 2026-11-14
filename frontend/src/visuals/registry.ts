// Visual plugins. Adding a visual means adding a folder here with a
// manifest.json and a Render.tsx; nothing else needs to change.
import { type ComponentType, lazy, type LazyExoticComponent } from "react";
import type { Manifest } from "../lib/dashboard";
import type { RenderProps } from "./types";

const manifestModules = import.meta.glob<Manifest>("./*/manifest.json", { eager: true, import: "default" });
const renderModules = import.meta.glob<{ default: ComponentType<RenderProps> }>("./*/Render.tsx");

const idOf = (path: string) => path.split("/")[1]!;

export const manifests: Record<string, Manifest> = Object.fromEntries(
  Object.entries(manifestModules).map(([path, manifest]) => [idOf(path), manifest]),
);

export const renderers: Record<string, LazyExoticComponent<ComponentType<RenderProps>>> = Object.fromEntries(
  Object.entries(renderModules).map(([path, load]) => [idOf(path), lazy(load)]),
);

export const visualList: Manifest[] = Object.values(manifests).sort((a, b) => a.name.localeCompare(b.name));
