// The in-app assistant: a person's conversation about a dashboard, and the live
// stream of a request while it runs.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut, errorOf, writeHeaders } from "./api";
import { url } from "./runtime";

export interface Step {
  callId: string;
  tool: string;
  title: string;
  input: string;
  ok?: boolean;
  output?: string;
  ms?: number;
  writes?: boolean;
  /** Dashboard versions on either side of a change the step made. */
  version?: { before: number | null; after: number | null };
}

export interface Summary {
  outcome: "done" | "stopped" | "timed_out" | "budget" | "refused" | "empty" | "error";
  provider: string;
  model: string;
  modelCalls: number;
  steps: number;
  inputTokens: number;
  cachedTokens: number;
  outputTokens: number;
  ms: number;
  versionBefore: number | null;
  versionAfter: number | null;
}

interface EntryBase {
  id: number;
  createdAt: string | null;
}

export type Entry =
  | (EntryBase & { kind: "request"; data: { text: string; selected: string | null } })
  | (EntryBase & { kind: "text" | "update"; data: { text: string; partial?: boolean } })
  | (EntryBase & { kind: "step"; data: Step })
  | (EntryBase & { kind: "notice"; data: { level: "info" | "warning" | "error"; text: string } })
  | (EntryBase & { kind: "done"; data: Summary });

export type ProviderId = "anthropic" | "openai" | "ollama";

/** How each provider is named in the panel. */
export const PROVIDER_NAMES: Record<ProviderId, string> = { anthropic: "Claude", openai: "OpenAI", ollama: "Ollama" };

/** Something a person may use in the assistant: their own key, the workspace's key or Ollama. */
export interface AssistantOption {
  id: string;
  source: "own" | "workspace" | "ollama";
  provider: ProviderId;
  label: string;
  /** The default model; null when the person has to name one. */
  model: string | null;
  /** The admin chose the model (the workspace key, Ollama). */
  fixedModel: boolean;
  keyId: string | null;
  last4: string | null;
}

export interface AssistantState {
  /** What the next request uses; null when the person has nothing to use. */
  provider: ProviderId | null;
  model: string | null;
  options: AssistantOption[];
  choice: { option: string; model: string | null } | null;
  /** The remembered choice can no longer be used (a grant taken away, a key deleted): choose again. */
  notice: string | null;
  /** Why there is nothing to use, and what to do about it. */
  unavailable: string | null;
  budgets: { steps: number; outputTokens: number; seconds: number };
  conversation: {
    id: string;
    provider: string;
    model: string;
    inputTokens: number;
    outputTokens: number;
    createdAt: string | null;
  } | null;
  entries: Entry[];
  /** A request is still running (perhaps started in another tab). */
  running: boolean;
  /** The provider or model changed since: the next request starts a new conversation. */
  outdated: boolean;
}

/** The conversation. While a request runs that this tab is not streaming (from
 * another tab, or before a reload), it is polled until the request is done. */
export function useAssistant(dashboardId: string, streaming: boolean) {
  return useQuery({
    queryKey: ["assistant", dashboardId],
    queryFn: () => apiGet<AssistantState>(`/api/dashboards/${dashboardId}/assistant`),
    refetchInterval: (query) => (!streaming && query.state.data?.running ? 2000 : false),
  });
}

export function useNewConversation(dashboardId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<AssistantState>(`/api/dashboards/${dashboardId}/assistant/conversations`),
    onSuccess: (state) => client.setQueryData(["assistant", dashboardId], state),
  });
}

export function useStopAssistant(dashboardId: string) {
  return useMutation({
    mutationFn: () => apiPost<{ stopping: boolean }>(`/api/dashboards/${dashboardId}/assistant/stop`),
  });
}

/** Remember what the person uses: an option, and a model for their own key. */
export function useSetChoice() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (choice: { option: string; model: string | null }) => apiPut<{ option: string; model: string | null }>("/api/assistant/choice", choice),
    onSuccess: () => client.invalidateQueries({ queryKey: ["assistant"] }),
  });
}

// ---- a person's own API keys (Account page) ---------------------------------------

export interface ApiKey {
  id: string;
  provider: "anthropic" | "openai";
  providerName: string;
  label: string;
  last4: string;
  defaultModel: string | null;
  lastTestOk: boolean | null;
  lastTestMessage: string | null;
  lastTestedAt: string | null;
  lastUsedAt: string | null;
  createdAt: string | null;
}

export interface UsageRow {
  source: "own" | "workspace" | "ollama";
  provider: ProviderId;
  keyId: string | null;
  keyLabel: string | null;
  requests: number;
  inputTokens: number;
  outputTokens: number;
  last30Days: { requests: number; inputTokens: number; outputTokens: number };
  lastAt: string | null;
}

export interface ApiKeys {
  /** An admin may switch personal keys off: kept, but neither used nor added. */
  enabled: boolean;
  keys: ApiKey[];
  workspaceKey: boolean;
  ollama: boolean;
  usage: UsageRow[];
}

export interface KeyTest {
  ok: boolean;
  message: string;
  models: { id: string; name: string }[];
  key: ApiKey;
}

export const useApiKeys = (enabled = true) =>
  useQuery({ queryKey: ["api-keys"], queryFn: () => apiGet<ApiKeys>("/api/account/api-keys"), enabled });

function useKeyMutation<T, V>(mutationFn: (variables: V) => Promise<T>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["api-keys"] });
      void client.invalidateQueries({ queryKey: ["assistant"] });
    },
  });
}

export const useAddKey = () =>
  useKeyMutation((body: { provider: string; label: string; key: string; defaultModel: string | null }) =>
    apiPost<ApiKey>("/api/account/api-keys", body),
  );
export const useChangeKey = () =>
  useKeyMutation(({ id, ...body }: { id: string; label?: string; defaultModel?: string | null }) =>
    apiPatch<ApiKey>(`/api/account/api-keys/${id}`, body),
  );
export const useDeleteKey = () => useKeyMutation((id: string) => apiDelete(`/api/account/api-keys/${id}`));
export const useTestKey = () => useKeyMutation((id: string) => apiPost<KeyTest>(`/api/account/api-keys/${id}/test`));
/** One short request to the key's default model: billed, so it shows the account can pay. */
export const useSendTestMessage = () =>
  useKeyMutation((id: string) => apiPost<KeyTest>(`/api/account/api-keys/${id}/test-message`, {}));

/** The models a key may use, asked of its provider when needed. */
export const useKeyModels = (keyId: string | null) =>
  useQuery({
    queryKey: ["key-models", keyId],
    queryFn: () => apiGet<{ models: { id: string; name: string }[] }>(`/api/account/api-keys/${keyId}/models`),
    enabled: Boolean(keyId),
    staleTime: 5 * 60_000,
    retry: false,
  });

// ---- admins: who may use the workspace key, and personal keys on or off -------------

export interface AssistantAccess {
  byoKeys: boolean;
  workspaceKey: { configured: boolean; model: string };
  ollama: { enabled: boolean; model: string };
  people: {
    id: string;
    displayName: string;
    email: string;
    role: string;
    isActive: boolean;
    /** The person's own setting: true, false, or null to follow their groups. */
    grant: boolean | null;
    /** Whether they may use it now, groups included. */
    workspaceKey: boolean;
    keys: Partial<Record<"anthropic" | "openai", number>>;
  }[];
  groups: { id: string; name: string; grant: boolean | null }[];
}

export const useAssistantAccess = () =>
  useQuery({ queryKey: ["assistant-access"], queryFn: () => apiGet<AssistantAccess>("/api/assistant-access") });

function useAccessMutation<V>(mutationFn: (variables: V) => Promise<unknown>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["assistant"] });
      void client.invalidateQueries({ queryKey: ["api-keys"] });
      // Still saving until the list shows the change, so a toggle never flicks back.
      return client.invalidateQueries({ queryKey: ["assistant-access"] });
    },
  });
}

export const useSetByoKeys = () =>
  useAccessMutation((byoKeys: boolean) => apiPut("/api/assistant-access/settings", { byoKeys }));
export const useSetGrant = () =>
  useAccessMutation((body: { grantee: string; allowed: boolean | null }) => apiPut("/api/assistant-access/grants", body));

export type LiveEvent =
  | { event: "start"; data: { conversationId: string; entry: Entry } }
  | { event: "entry"; data: Entry }
  | { event: "delta"; data: { block: number; kind: "text" | "update"; text: string } }
  | { event: "thinking"; data: { block: number } }
  | { event: "call"; data: { block: number; id: string; tool: string } }
  | { event: "step_started"; data: Step }
  | { event: "retry"; data: Record<string, never> }
  | { event: "done"; data: { outcome: string } };

/** One server-sent event, from the text between two blank lines. */
export function parseEvent(chunk: string): LiveEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith(":")) continue; // keep-alive comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  return { event, data: JSON.parse(data.join("\n")) } as LiveEvent;
}

/** Send a request to the assistant and report each event of its stream. Resolves
 * when the stream ends; the request itself goes on even if the stream is cut. */
export async function sendToAssistant(
  dashboardId: string,
  body: { text: string; selectedItemId: string | null },
  onEvent: (event: LiveEvent) => void,
): Promise<void> {
  const response = await fetch(url(`/api/dashboards/${dashboardId}/assistant/messages`), {
    method: "POST",
    headers: { Accept: "text/event-stream", "Content-Type": "application/json", ...writeHeaders() },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!response.ok || !response.body) throw await errorOf(response);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let end = buffer.indexOf("\n\n");
    while (end >= 0) {
      const parsed = parseEvent(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (parsed) onEvent(parsed);
      end = buffer.indexOf("\n\n");
    }
  }
}

/** What a request in progress has shown so far, built up from its events. */
export interface Live {
  entries: Entry[];
  /** Text the model is writing now, by block. */
  blocks: Record<number, { kind: "text" | "update"; text: string }>;
  thinking: boolean;
  /** A tool call being written, then one being run. */
  writing: string | null;
  running: Step | null;
}

export const NO_LIVE: Live = { entries: [], blocks: {}, thinking: false, writing: null, running: null };

export function applyEvent(live: Live, { event, data }: LiveEvent): Live {
  switch (event) {
    case "start":
      return { ...NO_LIVE, entries: [data.entry] };
    case "entry":
      // A stored entry replaces whatever was streaming for the reply it belongs to.
      return { ...live, entries: [...live.entries, data], blocks: {}, thinking: false, writing: null, running: data.kind === "step" ? null : live.running };
    case "delta": {
      const block = live.blocks[data.block];
      const text = (block?.text ?? "") + data.text;
      return { ...live, thinking: false, blocks: { ...live.blocks, [data.block]: { kind: data.kind, text } } };
    }
    case "thinking":
      return { ...live, thinking: true };
    case "call":
      return { ...live, thinking: false, writing: data.tool };
    case "step_started":
      return { ...live, writing: null, thinking: false, running: data };
    case "retry":
      return { ...live, blocks: {} };
    default:
      return live;
  }
}
