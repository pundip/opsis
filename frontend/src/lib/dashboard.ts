// Types and data hooks for dashboards.
import { keepPreviousData, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { CLIENT_ID, apiDelete, apiGet, apiPost, apiPut } from "./api";
import { url } from "./runtime";
import type { Field, Grain, ModelDetail, ResultColumn, SemanticQuery } from "./semantic";
import type { ThemeSpec } from "./theme";

export type Axis = "left" | "right";
export type Binding = string | { field: string; grain?: Grain; axis?: Axis };

export interface Position {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Item {
  id: string;
  visual: string;
  title?: string;
  position: Position;
  dataset?: string;
  bindings?: Record<string, Binding[]>;
  filters?: SemanticQuery["filters"];
  segments?: string[];
  sort?: { field: string; dir?: "asc" | "desc" }[];
  limit?: number;
  config?: Record<string, unknown>;
}

export type FilterType = "select" | "multiSelect" | "dateRange" | "search";

export interface FilterControl {
  id: string;
  type: FilterType;
  field: string;
  label?: string;
  default?: unknown;
  targets?: string[];
}

export interface Spec {
  specVersion: 1;
  title: string;
  description?: string;
  theme?: ThemeSpec;
  filters?: FilterControl[];
  layout: { columns: number; rowHeight: number };
  items: Item[];
  interactions?: { crossFilter?: boolean };
}

export interface Role {
  kind: "dimension" | "measure" | "any";
  min: number;
  max: number;
  label?: string;
  types?: string[];
  /** Measures in this role may choose the left or right value axis. */
  axis?: boolean;
}

export interface Manifest {
  id: string;
  name: string;
  version: string;
  description: string;
  roles: Record<string, Role>;
  configSchema: { type: "object"; properties?: Record<string, { default?: unknown }> };
  aiHints?: string;
  defaultSize?: { w: number; h: number };
  minSize?: { w: number; h: number };
  capabilities?: { crossFilter?: boolean; drillDown?: boolean };
}

export interface DashboardSummary {
  id: string;
  title: string;
  version: number;
  publishedVersion: number | null;
  hasUnpublishedChanges: boolean;
  ownerName: string | null;
  updatedAt: string;
  canEdit?: boolean;
  access?: "admin" | "owner" | "editor" | "viewer" | null;
}

export interface DashboardDetail extends DashboardSummary {
  source: "draft" | "published";
  spec: Spec;
  canEdit: boolean;
  canPublish: boolean;
  canShare: boolean;
  isOwner: boolean;
  access: "admin" | "owner" | "editor" | "viewer" | null;
}

export interface DashboardVersion {
  version: number;
  authorType: string;
  authorName: string;
  message: string | null;
  createdAt: string;
  current: boolean;
  published: boolean;
}

export interface CrossFilter {
  field: string;
  values: (string | number | boolean)[];
  source: string;
  label: string;
}

export interface ViewState {
  filters: Record<string, unknown>;
  cross: CrossFilter[];
  grains: Record<string, Grain>;
}

export const EMPTY_STATE: ViewState = { filters: {}, cross: [], grains: {} };

export interface ItemData {
  itemId: string;
  empty?: boolean;
  columns: ResultColumn[];
  rows: unknown[][];
  rowCount: number;
  truncated: boolean;
  rlsBypassed: boolean;
  comparison?: { mode: string; range: { from: string; to: string }; rows: unknown[][] };
  query?: SemanticQuery;
}

/** Who row-level security sees when an admin uses View as. */
export type ViewAs = { label: string } & ({ userId: string } | { role: string; attributes: Record<string, unknown> });

export type DataSource =
  | { kind: "draft" | "published"; viewAs?: ViewAs | null }
  | { kind: "inline"; spec: Spec }
  // A public share link: the token is all the API needs.
  | { kind: "link"; token: string };

function viewAsBody(source: DataSource): Record<string, unknown> | undefined {
  if ((source.kind !== "draft" && source.kind !== "published") || !source.viewAs) return undefined;
  // The label is for the screen only; the API needs the rest.
  return "userId" in source.viewAs
    ? { userId: source.viewAs.userId }
    : { role: source.viewAs.role, attributes: source.viewAs.attributes };
}

export const bindingField = (binding: Binding) => (typeof binding === "string" ? binding : binding.field);
export const bindingGrain = (binding: Binding) => (typeof binding === "string" ? undefined : binding.grain);
export const bindingAxis = (binding: Binding): Axis => (typeof binding === "string" ? "left" : (binding.axis ?? "left"));

/** Fields bound to a role, in order. */
export function bound(item: Item, role: string): string[] {
  return (item.bindings?.[role] ?? []).map(bindingField);
}

export function newId(prefix: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  for (let n = 1; ; n++) {
    const id = `${prefix}_${n}`;
    if (!used.has(id)) return id;
  }
}

/** Default config values declared in a manifest. */
export function defaultConfig(manifest: Manifest): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const [key, property] of Object.entries(manifest.configSchema.properties ?? {})) {
    if (property.default !== undefined) config[key] = property.default;
  }
  return config;
}

/** The parts of a spec that change an item's data. Layout and titles do not. */
function dataFingerprint(spec: Spec, item: Item): string {
  const rest: Partial<Item> = { ...item };
  delete rest.position;
  delete rest.title;
  const comparison = (item.config as { comparison?: string } | undefined)?.comparison;
  return JSON.stringify([{ ...rest, config: comparison }, spec.filters, spec.interactions]);
}

export function useVisuals() {
  return useQuery({ queryKey: ["visuals"], queryFn: () => apiGet<Manifest[]>("/api/visuals"), staleTime: Infinity });
}

export function useSchemas() {
  return useQuery({
    queryKey: ["schemas"],
    queryFn: () => apiGet<Record<string, object>>("/api/schemas"),
    staleTime: Infinity,
  });
}

export function useDashboards() {
  return useQuery({ queryKey: ["dashboards"], queryFn: () => apiGet<DashboardSummary[]>("/api/dashboards") });
}

export function useDashboard(id: string) {
  return useQuery({
    queryKey: ["dashboards", id],
    queryFn: () => apiGet<DashboardDetail>(`/api/dashboards/${id}`),
  });
}

export function useCreateDashboard() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (title: string) => apiPost<DashboardDetail>("/api/dashboards", { title }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["dashboards"] }),
  });
}

export function useDeleteDashboard() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiDelete(`/api/dashboards/${id}`),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["dashboards"] }),
  });
}

function useDashboardMutation<Input>(id: string, run: (input: Input) => Promise<DashboardDetail>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: (saved) => {
      client.setQueryData(["dashboards", id], saved);
      void client.invalidateQueries({ queryKey: ["dashboards"], exact: true });
      void client.invalidateQueries({ queryKey: ["dashboard-versions", id] });
      void client.invalidateQueries({ queryKey: ["item-data", id] });
    },
  });
}

export const useSaveDashboard = (id: string) =>
  useDashboardMutation(id, (input: { spec: Spec; baseVersion: number; message?: string }) =>
    apiPut<DashboardDetail>(`/api/dashboards/${id}`, input),
  );

export const usePublishDashboard = (id: string) =>
  useDashboardMutation(id, (publish: boolean) =>
    apiPost<DashboardDetail>(`/api/dashboards/${id}/${publish ? "publish" : "unpublish"}`),
  );

export const useRestoreDashboard = (id: string) =>
  useDashboardMutation(id, (version: number) =>
    apiPost<DashboardDetail>(`/api/dashboards/${id}/versions/${version}/restore`),
  );

/** A change to a dashboard made somewhere else: another tab, another person, or the assistant. */
export interface DashboardEvent {
  kind: "hello" | "version_created" | "published";
  version: number;
  publishedVersion?: number | null;
  authorType?: "user" | "agent" | "system";
  authorName?: string;
  message?: string | null;
}

/**
 * Listen to a dashboard's live event stream while the editor is open.
 * Events caused by this tab are dropped. The browser reconnects by itself, and
 * the first event after each connect ("hello") carries the current version so
 * nothing is missed in between.
 */
export function useDashboardEvents(id: string, onEvent: (event: DashboardEvent) => void): void {
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource(url(`/api/dashboards/${id}/events`));
    const listen = (kind: DashboardEvent["kind"]) =>
      source.addEventListener(kind, (message) => {
        try {
          const data = JSON.parse((message as MessageEvent<string>).data) as DashboardEvent & { clientId?: string | null };
          if (data.clientId && data.clientId === CLIENT_ID) return;
          handler.current({ ...data, kind });
        } catch {
          // A malformed event is ignored; the next one corrects the state.
        }
      });
    listen("hello");
    listen("version_created");
    listen("published");
    return () => source.close();
  }, [id]);
}

export function useDashboardVersions(id: string) {
  return useQuery({
    queryKey: ["dashboard-versions", id],
    queryFn: () => apiGet<DashboardVersion[]>(`/api/dashboards/${id}/versions`),
  });
}

export function useDashboardVersion(id: string, version: number | null) {
  return useQuery({
    queryKey: ["dashboard-versions", id, version],
    queryFn: () => apiGet<{ version: number; spec: Spec }>(`/api/dashboards/${id}/versions/${version}`),
    enabled: version !== null,
    staleTime: Infinity,
  });
}

export function useValidateSpec() {
  return useMutation({
    mutationFn: (spec: unknown) =>
      apiPost<{ valid: boolean; problems: { path: string; message: string }[] }>("/api/dashboards/validate", { spec }),
  });
}

export function useItemData(dashboardId: string, item: Item, spec: Spec, source: DataSource, state: ViewState) {
  // An item is not affected by its own cross-filter, or by other items' drill level.
  const relevant = {
    filters: state.filters,
    cross: state.cross.filter((c) => c.source !== item.id).map(({ field, values, source: from }) => ({ field, values, source: from })),
    grains: state.grains[item.id] ? { [item.id]: state.grains[item.id] } : {},
  };
  const key = source.kind === "inline" ? dataFingerprint(spec, item) : source.kind;
  const viewAs = viewAsBody(source);
  return useQuery({
    queryKey: ["item-data", dashboardId, item.id, key, JSON.stringify(relevant), JSON.stringify(viewAs ?? null)],
    queryFn: () =>
      source.kind === "link"
        ? apiPost<ItemData>(`/api/public/links/${source.token}/data`, { itemId: item.id, state: relevant })
        : apiPost<ItemData>(`/api/dashboards/${dashboardId}/data`, {
            itemId: item.id,
            source: source.kind,
            spec: source.kind === "inline" ? source.spec : undefined,
            state: relevant,
            viewAs,
          }),
    placeholderData: keepPreviousData,
    retry: false,
    staleTime: 30_000,
  });
}

export function useFilterOptions(dashboardId: string, filter: FilterControl, source: DataSource, enabled: boolean) {
  return useQuery({
    queryKey: ["filter-options", dashboardId, filter.id, filter.field, source.kind, JSON.stringify(viewAsBody(source) ?? null)],
    queryFn: () =>
      apiPost<{ values: (string | number | boolean)[]; truncated: boolean }>(
        source.kind === "link"
          ? `/api/public/links/${source.token}/filter-options`
          : `/api/dashboards/${dashboardId}/filter-options`,
        source.kind === "link"
          ? { filterId: filter.id }
          : {
              filterId: filter.id,
              source: source.kind,
              spec: source.kind === "inline" ? source.spec : undefined,
              viewAs: viewAsBody(source),
            },
      ),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

/** Model fields for several datasets at once (the editor's field pickers). */
export function useFieldsFor(datasets: string[]): Record<string, Field[]> {
  const results = useQueries({
    queries: datasets.map((dataset) => ({
      queryKey: ["models", dataset],
      queryFn: () => apiGet<ModelDetail>(`/api/models/${dataset}`),
      staleTime: 60_000,
    })),
  });
  const fields: Record<string, Field[]> = {};
  datasets.forEach((dataset, index) => {
    fields[dataset] = results[index]?.data?.fields ?? [];
  });
  return fields;
}
