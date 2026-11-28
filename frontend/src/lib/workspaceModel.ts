// The workspace model (V2): one model for the workspace, with its tables, the
// relationships between them and the Model view's layout. Types and data hooks.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch, apiPost, apiPut } from "./api";
import type { ModelWarning, Problem } from "./semantic";

export interface RelationshipEnd {
  table: string;
  columns: string[];
}

export interface Relationship {
  from: RelationshipEnd; // the many side
  to: RelationshipEnd; // the one side
  cardinality?: "many_to_one" | "one_to_one";
  crossFilter?: "single" | "both";
  securityFilter?: "single" | "both";
  active?: boolean;
  joinType?: "left" | "inner";
  description?: string;
}

/** A relationship with every setting written out (the description stays optional). */
export type FullRelationship = Required<Omit<Relationship, "description">> & { description?: string };

export const DATA_CATEGORIES = [
  "date",
  "currency",
  "postcode",
  "country",
  "state",
  "city",
  "address",
  "latitude",
  "longitude",
  "url",
  "email",
] as const;

export interface DimensionEntry {
  column?: string;
  expr?: string;
  type?: "string" | "number" | "boolean" | "time";
  label?: string;
  description?: string;
  hidden?: boolean;
  format?: string;
  dataCategory?: (typeof DATA_CATEGORIES)[number];
  [key: string]: unknown;
}

export interface MeasureEntry {
  agg?: "sum" | "count" | "count_distinct" | "avg" | "min" | "max";
  column?: string;
  expr?: string;
  filter?: string;
  format?: string;
  label?: string;
  description?: string;
  hidden?: boolean;
  relationship?: string;
  [key: string]: unknown;
}

export interface TableEntry {
  source?: { dataset: string };
  label?: string;
  description?: string;
  hidden?: boolean;
  dateTable?: { column: string };
  dimensions?: Record<string, DimensionEntry>;
  measures?: Record<string, MeasureEntry>;
  [key: string]: unknown;
}

export interface WorkspaceDocument {
  modelVersion: 2;
  label?: string;
  description?: string;
  tables: Record<string, TableEntry>;
  relationships: Record<string, Relationship>;
}

export interface TableInfo {
  name: string;
  datasetId?: string;
  label: string;
  hasData: boolean;
  restricted: boolean;
  primaryKey: string[];
  columns: { name: string; type: string }[];
  rowCount: number | null;
  generated: boolean;
}

export interface Position {
  x: number;
  y: number;
}

export interface Placement extends Position {
  collapsed?: boolean;
}

export interface Layout {
  tables?: Record<string, Placement>;
  zoom?: number;
}

export interface WorkspaceModel {
  version: number;
  document: WorkspaceDocument;
  yaml: string;
  layout: Layout;
  tables: TableInfo[];
  problems: Problem[];
  warnings: ModelWarning[];
  updatedAt: string | null;
}

export interface PatchOp {
  op: "add" | "remove" | "replace";
  path: string;
  value?: unknown;
}

export interface KeySide {
  table: string;
  columns: string[];
  rows: number;
  emptyKeys: number;
  distinctKeys: number;
  unique: boolean;
}

export interface KeyCheck {
  id: string;
  relationship: FullRelationship;
  swapped: boolean;
  confidence: number;
  evidence: string[];
  warnings: { code: string; message: string }[];
  keys: { from: KeySide; to: KeySide };
  overlap: { sampled: number; matched: number; share: number } | null;
  inactiveBecause?: string;
  note: string;
}

export interface Proposal {
  id: string;
  relationship: FullRelationship;
  confidence: number;
  evidence: string[];
  inactiveBecause?: string;
}

export interface Suggestions {
  proposals: Proposal[];
  pairsChecked: number;
  note: string;
}

export interface DateTableResult {
  dataset: { id: string; name: string; label: string };
  connection: { id: string; name: string };
  table: string;
  start: string;
  end: string;
  rows: number | null;
  version: number;
  proposals: Proposal[];
  note: string;
}

export interface Validation {
  valid: boolean;
  problems: Problem[];
  existing: Problem[];
  warnings: ModelWarning[];
}

export interface VersionEntry {
  version: number;
  authorType: "user" | "agent" | "system";
  authorName: string;
  message: string | null;
  createdAt: string;
  current: boolean;
}

export interface VersionDiff {
  from: number;
  to: number;
  tables: string[];
  relationships: string[];
  ops: PatchOp[];
}

export const MODEL_KEY = ["workspace-model"] as const;

/** Every view of the model reads the stored model: after a change they read it again. */
function useSaved() {
  const client = useQueryClient();
  return (saved: WorkspaceModel) => {
    client.setQueryData(MODEL_KEY, saved);
    void client.invalidateQueries({ queryKey: ["models"] });
    void client.invalidateQueries({ queryKey: ["model-versions"] });
    void client.invalidateQueries({ queryKey: ["workspace-model-versions"] });
  };
}

export function useWorkspaceModel() {
  return useQuery({ queryKey: MODEL_KEY, queryFn: () => apiGet<WorkspaceModel>("/api/model") });
}

export function usePatchModel() {
  const saved = useSaved();
  return useMutation({
    mutationFn: (input: { ops: PatchOp[]; baseVersion: number; message?: string }) =>
      apiPatch<WorkspaceModel>("/api/model", input),
    onSuccess: saved,
  });
}

export function usePutModel() {
  const saved = useSaved();
  return useMutation({
    mutationFn: (input: { yaml: string; baseVersion: number; message?: string }) =>
      apiPut<WorkspaceModel>("/api/model", input),
    onSuccess: saved,
  });
}

export function useValidateModel() {
  return useMutation({
    mutationFn: (input: { yaml?: string; ops?: PatchOp[]; baseVersion?: number }) =>
      apiPost<Validation>("/api/model/validate", input),
  });
}

export function useSaveLayout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (layout: Layout) => apiPut<{ layout: Layout }>("/api/model/layout", { layout }),
    onSuccess: (result) =>
      client.setQueryData<WorkspaceModel>(MODEL_KEY, (old) => old && { ...old, layout: result.layout }),
  });
}

export function useModelVersions(enabled: boolean) {
  return useQuery({
    queryKey: ["workspace-model-versions"],
    queryFn: () => apiGet<VersionEntry[]>("/api/model/versions"),
    enabled,
  });
}

export function useModelDiff(first: number, second: number | null) {
  return useQuery({
    queryKey: ["workspace-model-diff", first, second],
    queryFn: () => apiGet<VersionDiff>(`/api/model/diff?first=${first}&second=${second}`),
    enabled: second !== null,
  });
}

export function useRestoreVersion() {
  const saved = useSaved();
  return useMutation({
    mutationFn: (version: number) => apiPost<WorkspaceModel>(`/api/model/versions/${version}/restore`),
    onSuccess: saved,
  });
}

export function useKeyCheck() {
  return useMutation({
    mutationFn: (pair: { from: RelationshipEnd; to: RelationshipEnd }) =>
      apiPost<KeyCheck>("/api/model/key-check", pair),
  });
}

export function useSuggestions() {
  return useMutation({
    mutationFn: (tables?: string[]) => apiPost<Suggestions>("/api/model/suggestions", { tables }),
  });
}

export function useGenerateDateTable() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { name?: string; label?: string; start?: string }) =>
      apiPost<DateTableResult>("/api/model/date-table", input),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: MODEL_KEY });
      void client.invalidateQueries({ queryKey: ["models"] });
      void client.invalidateQueries({ queryKey: ["datasets"] });
      void client.invalidateQueries({ queryKey: ["connections"] });
      void client.invalidateQueries({ queryKey: ["workspace-model-versions"] });
    },
  });
}

/** A JSON Pointer token: "~" and "/" escaped (RFC 6901). */
export function pointer(...parts: string[]): string {
  return parts.map((part) => `/${part.replaceAll("~", "~0").replaceAll("/", "~1")}`).join("");
}

/** A relationship with every property written out, as the server stores it. */
export function complete(rel: Relationship): FullRelationship {
  return {
    ...rel,
    from: rel.from,
    to: rel.to,
    cardinality: rel.cardinality ?? "many_to_one",
    crossFilter: rel.crossFilter ?? "single",
    securityFilter: rel.securityFilter ?? "single",
    active: rel.active ?? true,
    joinType: rel.joinType ?? "left",
  };
}

export function describeRelationship(rel: Relationship): string {
  return `${rel.from.table} (${rel.from.columns.join(", ")}) to ${rel.to.table} (${rel.to.columns.join(", ")})`;
}
