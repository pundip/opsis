// Types and data hooks for connections, datasets and jobs.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPatch, apiPost } from "./api";

export interface JsonSchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: string[];
  minimum?: number;
  maximum?: number;
  "x-secret"?: boolean;
  "x-upload"?: boolean;
  /** The value can also be read from a local file of this type (".json"), in the browser. */
  "x-file-text"?: string;
  /** Show the field only while other fields have these values, for example {"auth": "oauth"}. */
  "x-show-when"?: Record<string, unknown>;
  /** Labels for the values of an enum. */
  "x-enum-labels"?: Record<string, string>;
}

export interface ConfigSchema {
  type: "object";
  required?: string[];
  properties: Record<string, JsonSchemaProperty>;
}

export interface Connector {
  id: string;
  name: string;
  version: string;
  configSchema: ConfigSchema;
}

export interface TestResult {
  ok: boolean;
  message: string;
}

export interface Connection {
  id: string;
  name: string;
  connectorId: string;
  connectorName: string;
  config: Record<string, unknown>;
  secretFields: string[];
  secretsReadable: boolean;
  lastTest: (TestResult & { at: string }) | null;
  datasetCount: number;
  createdAt: string;
  /** A saved sign-in (Google Sheets through OAuth). Never contains the token. */
  auth?: AuthState | null;
}

export interface AuthState {
  method: "oauth" | "service_account";
  status: "connected" | "needs_reconnect" | "not_connected";
  account?: string | null;
  scopes?: string[];
  /** Whether the sign-in may list the account's spreadsheets (the Drive metadata scope). */
  canList?: boolean;
  connectedAt?: string;
  source?: "consent" | "token.json";
  error?: string;
  failedAt?: string;
  notifiedAt?: string;
}

/** A Google Sheets connection that signs in with the person's own Google account. */
export function usesGoogleAccount(connection: Connection): boolean {
  return connection.connectorId === "gsheets" && (connection.config.auth ?? "oauth") === "oauth";
}

export type ColumnType =
  | "string" | "integer" | "float" | "decimal" | "boolean" | "date" | "timestamp" | "json";

export interface Column {
  name: string;
  type: ColumnType;
  sourceType?: string | null;
}

export type RefreshMode = "full" | "append" | "merge";

export interface Stream {
  name: string;
  label: string;
  suggestedName: string;
  columns: Column[];
  primaryKey: string[];
  cursorCandidates: string[];
  supportedModes: RefreshMode[];
  rowCountEstimate: number | null;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Job {
  id: string;
  type: string;
  status: JobStatus;
  datasetId: string | null;
  full: boolean;
  attempts: number;
  error: string | null;
  result: { rowCount?: number; sizeBytes?: number; mode?: string; seconds?: number } | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface Snapshot {
  id: string;
  rowCount: number;
  sizeBytes: number;
  mode: RefreshMode;
  columns: Column[];
  createdAt: string;
  current: boolean;
}

export interface Dataset {
  /** With FEATURE_SCHEDULED_REFRESH: the refresh schedule, or null for none. */
  schedule?: import("./operations").Schedule | null;
  id: string;
  name: string;
  label: string;
  connectionId: string;
  connectionName: string | null;
  connectorId: string | null;
  stream: string;
  refreshMode: RefreshMode;
  cursorField: string | null;
  primaryKey: string[];
  columns: string[] | null;
  restricted?: boolean;
  /** The dataset's owner: the person who created it. */
  createdBy?: string | null;
  snapshot: Snapshot | null;
  lastJob: Job | null;
  createdAt: string;
}

export interface Preview {
  snapshotId: string;
  columns: Column[];
  rows: unknown[][];
  // Null when row-level security applies: the total would reveal how much is hidden.
  rowCount: number | null;
  limit: number;
  rlsApplied?: boolean;
}

export const isActive = (job: Job | null | undefined) =>
  job?.status === "queued" || job?.status === "running";

// Poll quickly while something is running, slowly otherwise.
const FAST = 1500;

export function useConnectors() {
  return useQuery({
    queryKey: ["connectors"],
    queryFn: () => apiGet<Connector[]>("/api/connectors"),
    staleTime: Infinity,
  });
}

export function useConnections() {
  return useQuery({
    queryKey: ["connections"],
    queryFn: () => apiGet<Connection[]>("/api/connections"),
  });
}

export function useStreams(connectionId: string | null) {
  return useQuery({
    queryKey: ["streams", connectionId],
    queryFn: () => apiGet<Stream[]>(`/api/connections/${connectionId}/streams`),
    enabled: connectionId !== null,
    retry: false,
    staleTime: 60_000,
  });
}

export function useDatasets() {
  return useQuery({
    queryKey: ["datasets"],
    queryFn: () => apiGet<Dataset[]>("/api/datasets"),
    refetchInterval: (query) => (query.state.data?.some((d) => isActive(d.lastJob)) ? FAST : false),
  });
}

export function useDataset(id: string) {
  return useQuery({
    queryKey: ["datasets", id],
    queryFn: () => apiGet<Dataset>(`/api/datasets/${id}`),
    refetchInterval: (query) => (isActive(query.state.data?.lastJob) ? FAST : false),
  });
}

export function usePreview(id: string, snapshotId: string | undefined) {
  return useQuery({
    queryKey: ["datasets", id, "preview", snapshotId],
    queryFn: () => apiGet<Preview>(`/api/datasets/${id}/preview?limit=100`),
    enabled: snapshotId !== undefined,
  });
}

export function useSnapshots(id: string, snapshotId: string | undefined) {
  return useQuery({
    queryKey: ["datasets", id, "snapshots", snapshotId],
    queryFn: () => apiGet<Snapshot[]>(`/api/datasets/${id}/snapshots`),
  });
}

/** One job, looked at again every moment while it is queued or running. */
export function useJob(id: string | null | undefined) {
  return useQuery({
    queryKey: ["jobs", "one", id],
    queryFn: () => apiGet<Job>(`/api/jobs/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => (query.state.data === undefined || isActive(query.state.data) ? FAST : false),
  });
}

export function useJobs(datasetId?: string) {
  return useQuery({
    queryKey: ["jobs", datasetId ?? "all"],
    queryFn: () =>
      apiGet<Job[]>(datasetId ? `/api/jobs?datasetId=${datasetId}&limit=20` : "/api/jobs?limit=100"),
    refetchInterval: (query) => (query.state.data?.some(isActive) ? FAST : 15_000),
  });
}

export interface ConnectionInput {
  name: string;
  connectorId: string;
  config: Record<string, unknown>;
}

export function useSaveConnection(id: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: ConnectionInput) =>
      id
        ? apiPatch<Connection>(`/api/connections/${id}`, { name: input.name, config: input.config })
        : apiPost<Connection>("/api/connections", input),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["connections"] });
      void client.invalidateQueries({ queryKey: ["streams"] });
    },
  });
}

export function useTestSettings() {
  return useMutation({
    mutationFn: (input: ConnectionInput & { connectionId: string | null }) =>
      apiPost<TestResult>("/api/connections/test", input),
  });
}

export function useTestConnection() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiPost<TestResult>(`/api/connections/${id}/test`),
    onSettled: () => void client.invalidateQueries({ queryKey: ["connections"] }),
  });
}

// ---- Google Sheets: the person's own Google account ------------------------------------

export interface GoogleStart {
  authUrl: string;
  /** direct: Google comes back to Opsis; paste: copy the address from the address bar. */
  mode: "direct" | "paste";
  redirectUri: string;
}

export interface Spreadsheet {
  id: string;
  name: string;
  modifiedTime: string | null;
  owner: string | null;
}

export const useGoogleStart = () =>
  useMutation({
    mutationFn: (id: string) => apiPost<GoogleStart>(`/api/connections/${id}/google/start`),
  });

export function useGoogleFinish() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (url: string) =>
      apiPost<{ connectionId: string; auth: AuthState }>("/api/connections/google/finish", { url }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["connections"] }),
  });
}

export function useGoogleToken(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (token: string) =>
      apiPost<{ connectionId: string; auth: AuthState }>(`/api/connections/${id}/google/token`, { token }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["connections"] }),
  });
}

export function useSpreadsheets(id: string, search: string, enabled: boolean) {
  return useQuery({
    queryKey: ["spreadsheets", id, search],
    queryFn: () =>
      apiGet<{ spreadsheets: Spreadsheet[] }>(
        `/api/connections/${id}/google/spreadsheets${search ? `?q=${encodeURIComponent(search)}` : ""}`,
      ),
    enabled,
    retry: false,
  });
}

export function useDeleteConnection() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiDelete(`/api/connections/${id}`),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["connections"] }),
  });
}

export interface DatasetInput {
  name: string;
  label: string;
  connectionId: string;
  stream: string;
  refreshMode: RefreshMode;
  cursorField: string | null;
  primaryKey: string[];
}

export function useCreateDataset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: DatasetInput) => apiPost<Dataset>("/api/datasets", input),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["datasets"] });
      void client.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

export function useRefreshDataset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, full }: { id: string; full?: boolean }) =>
      apiPost<Job>(`/api/datasets/${id}/refresh`, { full: full ?? false }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["datasets"] });
      void client.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

export function useDeleteDataset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiDelete(`/api/datasets/${id}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["datasets"] });
      void client.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}
