// Types and data hooks for semantic models and queries.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "./api";
import type { Column } from "./data";

export type Grain =
  | "day" | "week" | "month" | "quarter" | "year"
  | "fiscal_month" | "fiscal_quarter" | "fiscal_year";

export interface ModelSummary {
  dataset: string;
  label: string;
  hasData: boolean;
  saved: boolean;
  isStarter: boolean;
  dimensions: number;
  measures: number;
  joins: string[];
  rlsRules: number;
  updatedAt: string | null;
}

export interface Field {
  ref: string; // "gifts.total_amount"
  kind: "dimension" | "measure" | "segment";
  type: "string" | "number" | "boolean" | "time";
  label: string;
  dataset: string;
  grains?: Grain[];
  format?: string | null;
}

/** Something a model saves with, but that may give surprising numbers or access. */
export interface ModelWarning {
  code:
    | "duplicate_key"
    | "key_not_checked"
    | "ambiguous_path"
    | "key_types_differ"
    | "relationship_to_restricted";
  path: string;
  message: string;
}

export interface ModelDetail {
  dataset: { id: string; name: string; label: string; hasData: boolean; columns: Column[] };
  definition: Record<string, unknown>;
  yaml: string;
  fields: Field[];
  isStarter: boolean;
  saved: boolean;
  version: number;
  updatedAt: string | null;
  warnings: ModelWarning[];
}

export interface ModelVersion {
  version: number;
  authorType: "user" | "agent" | "system";
  authorName: string;
  message: string | null;
  createdAt: string;
  current: boolean;
}

export interface Problem {
  path: string;
  message: string;
}

export interface SemanticQuery {
  dataset: string;
  dimensions?: string[];
  measures?: string[];
  timeDimension?: { field: string; grain?: Grain; range?: { relative: string } };
  segments?: string[];
  filters?: { field: string; op: string; values?: (string | number | boolean)[] }[];
  sort?: { field: string; dir: "asc" | "desc" }[];
  limit?: number;
}

export interface ResultColumn {
  name: string;
  label: string | null;
  type: string;
  role: "dimension" | "measure" | "time";
  format: string | null;
  grain: Grain | null;
}

export interface QueryResult {
  columns: ResultColumn[];
  rows: unknown[][];
  rowCount: number;
  truncated: boolean;
  elapsedMs: number;
  cacheHit: boolean;
  rlsBypassed: boolean;
  sql?: string;
}

export interface PreviewAs {
  role: "viewer" | "editor" | "admin";
  attributes: Record<string, unknown>;
}

export function useModels() {
  return useQuery({ queryKey: ["models"], queryFn: () => apiGet<ModelSummary[]>("/api/models") });
}

export function useModel(dataset: string) {
  return useQuery({
    queryKey: ["models", dataset],
    queryFn: () => apiGet<ModelDetail>(`/api/models/${dataset}`),
  });
}

export function useSaveModel(dataset: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { yaml: string; baseVersion: number; message?: string }) =>
      apiPut<ModelDetail>(`/api/models/${dataset}`, input),
    onSuccess: (saved) => {
      client.setQueryData(["models", dataset], saved);
      void client.invalidateQueries({ queryKey: ["models"] });
      void client.invalidateQueries({ queryKey: ["model-versions", dataset] });
    },
  });
}

export function useModelVersions(dataset: string) {
  return useQuery({
    queryKey: ["model-versions", dataset],
    queryFn: () => apiGet<ModelVersion[]>(`/api/models/${dataset}/versions`),
  });
}

export function useRestoreModelVersion(dataset: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (version: number) =>
      apiPost<ModelDetail>(`/api/models/${dataset}/versions/${version}/restore`),
    onSuccess: (saved) => {
      client.setQueryData(["models", dataset], saved);
      void client.invalidateQueries({ queryKey: ["models"] });
      void client.invalidateQueries({ queryKey: ["model-versions", dataset] });
    },
  });
}

export function useValidateModel(dataset: string) {
  return useMutation({
    mutationFn: (yaml: string) =>
      apiPost<{ valid: boolean; problems: Problem[]; warnings: ModelWarning[] }>(
        `/api/models/${dataset}/validate`,
        { yaml },
      ),
  });
}

export function useStarterModel(dataset: string) {
  return useMutation({
    mutationFn: () => apiGet<{ yaml: string }>(`/api/models/${dataset}/starter`),
  });
}

export function useRunQuery() {
  return useMutation({
    mutationFn: (input: { query: SemanticQuery; previewAs?: PreviewAs }) =>
      apiPost<QueryResult>("/api/query", { ...input, includeSql: true }),
  });
}

export const RELATIVE_RANGES: [string, string][] = [
  ["", "All dates"],
  ["this_month", "This month"],
  ["last_month", "Last month"],
  ["last_30_days", "Last 30 days"],
  ["last_12_months", "Last 12 months"],
  ["this_year", "This calendar year"],
  ["last_year", "Last calendar year"],
  ["this_fiscal_year", "This financial year"],
  ["last_fiscal_year", "Last financial year"],
  ["fiscal_year_to_date", "Financial year to date"],
  ["this_fiscal_quarter", "This financial quarter"],
  ["last_fiscal_quarter", "Last financial quarter"],
];

export const GRAIN_LABELS: Record<Grain, string> = {
  day: "Day",
  week: "Week",
  month: "Month",
  quarter: "Quarter",
  year: "Year",
  fiscal_month: "Financial period",
  fiscal_quarter: "Financial quarter",
  fiscal_year: "Financial year",
};

/** One file of a models/ import and what happened to it. */
export interface ModelFileResult {
  file: string;
  dataset: string | null;
  status: "changed" | "unchanged" | "invalid" | "unknown_dataset" | "saved";
  problems?: { path: string; message: string }[];
  version?: number;
}

export interface ModelImport {
  applied: boolean;
  valid: boolean;
  files: ModelFileResult[];
}

/** Import a zip of a models/ folder, or .yml files. dryRun only checks them. */
export function useImportModels() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ files, dryRun }: { files: File[]; dryRun: boolean }) => {
      const form = new FormData();
      for (const file of files) form.append("files", file);
      return apiPost<ModelImport>(`/api/model-files?dryRun=${dryRun ? "true" : "false"}`, form);
    },
    onSuccess: (result) => {
      if (result.applied) void client.invalidateQueries({ queryKey: ["models"] });
    },
  });
}
