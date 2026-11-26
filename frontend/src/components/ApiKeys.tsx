// Your own API keys for the assistant (Account page): add, test, rename, choose a
// default model, delete. A key goes in once and never comes back: only its provider,
// its name and its last four characters are shown. Also what you have used, per key.
import { useState } from "react";
import {
  type ApiKey,
  type KeyTest,
  type UsageRow,
  PROVIDER_NAMES,
  useAddKey,
  useApiKeys,
  useChangeKey,
  useDeleteKey,
  useKeyModels,
  useTestKey,
} from "../lib/assistant";
import { timeAgo } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, Field, Input, ROW, Select, TD, TH } from "./ui";

const PROVIDERS = [
  { id: "anthropic", label: "Anthropic (Claude)", example: "claude-opus-5-5" },
  { id: "openai", label: "OpenAI", example: "gpt-5.5" },
] as const;

function TestResult({ result }: { result: KeyTest }) {
  return (
    <p
      role="status"
      className={`mt-2 rounded-md border px-3 py-2 text-sm ${
        result.ok
          ? "border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40"
          : "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40"
      }`}
    >
      {result.key.label}: {result.message}
    </p>
  );
}

function AddKey({ onTested }: { onTested: (result: KeyTest) => void }) {
  const add = useAddKey();
  const test = useTestKey();
  const [provider, setProvider] = useState<"anthropic" | "openai">("anthropic");
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [model, setModel] = useState("");
  const example = PROVIDERS.find((p) => p.id === provider)!.example;
  return (
    <form
      className="grid gap-3 sm:grid-cols-[11rem_1fr] sm:items-end"
      onSubmit={(event) => {
        event.preventDefault();
        add.mutate(
          { provider, label: label.trim(), key: key.trim(), defaultModel: model.trim() || null },
          {
            onSuccess: (made) => {
              setLabel("");
              setKey("");
              setModel("");
              // Test it at once: free, and it lists the models the key may use.
              test.mutate(made.id, { onSuccess: onTested });
            },
          },
        );
      }}
    >
      <Field label="Provider" htmlFor="key-provider">
        <Select id="key-provider" value={provider} onChange={(e) => setProvider(e.target.value as "anthropic" | "openai")}>
          {PROVIDERS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Name" htmlFor="key-label" hint="For you to tell your keys apart, for example Work.">
        <Input id="key-label" required maxLength={100} value={label} onChange={(e) => setLabel(e.target.value)} />
      </Field>
      <Field label="API key" htmlFor="key-secret" hint="It is kept encrypted and never shown again, not even to admins.">
        <Input id="key-secret" type="password" autoComplete="off" spellCheck={false} required value={key} onChange={(e) => setKey(e.target.value)} />
      </Field>
      <Field label="Default model" htmlFor="key-model" hint={`Optional, for example ${example}. You can also choose one in the assistant.`}>
        <Input id="key-model" className="font-mono" maxLength={200} value={model} onChange={(e) => setModel(e.target.value)} />
      </Field>
      <div className="sm:col-span-2">
        <ErrorNote error={add.error ?? test.error} />
        <Button type="submit" variant="primary" busy={add.isPending || test.isPending}>
          Add key
        </Button>
      </div>
    </form>
  );
}

function EditKey({ apiKey, onClose }: { apiKey: ApiKey; onClose: () => void }) {
  const change = useChangeKey();
  const models = useKeyModels(apiKey.id);
  const [label, setLabel] = useState(apiKey.label);
  const [model, setModel] = useState(apiKey.defaultModel ?? "");
  const listId = `models-${apiKey.id}`;
  return (
    <form
      className="mt-2 grid gap-2 rounded-md bg-zinc-50 p-3 sm:grid-cols-2 dark:bg-zinc-950"
      onSubmit={(event) => {
        event.preventDefault();
        change.mutate({ id: apiKey.id, label: label.trim(), defaultModel: model.trim() || null }, { onSuccess: onClose });
      }}
    >
      <Field label="Name" htmlFor={`label-${apiKey.id}`}>
        <Input id={`label-${apiKey.id}`} required maxLength={100} value={label} onChange={(e) => setLabel(e.target.value)} />
      </Field>
      <Field label="Default model" htmlFor={`model-${apiKey.id}`} hint={models.isError ? "The model list could not be loaded: type a model." : undefined}>
        <Input id={`model-${apiKey.id}`} className="font-mono" list={listId} maxLength={200} value={model} onChange={(e) => setModel(e.target.value)} />
        <datalist id={listId}>
          {(models.data?.models ?? []).map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.name}
            </option>
          ))}
        </datalist>
      </Field>
      <div className="flex gap-2 sm:col-span-2">
        <ErrorNote error={change.error} />
        <Button type="submit" variant="primary" busy={change.isPending}>
          Save
        </Button>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function usageName(row: UsageRow): string {
  if (row.source === "workspace") return "The workspace's Claude key";
  if (row.source === "ollama") return "Ollama";
  return row.keyLabel ? `${row.keyLabel}` : "A deleted key";
}

function Usage({ rows }: { rows: UsageRow[] }) {
  if (rows.length === 0) return <Empty>You have not used the assistant yet.</Empty>;
  const tokens = (input: number, output: number) => `${input.toLocaleString()} in, ${output.toLocaleString()} out`;
  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Your assistant use">
        <thead>
          <tr>
            <th className={TH}>Key</th>
            <th className={TH}>Provider</th>
            <th className={TH}>Requests</th>
            <th className={TH}>Tokens</th>
            <th className={TH}>Last 30 days</th>
            <th className={TH}>Last used</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.source}-${row.keyId ?? row.keyLabel ?? ""}-${row.provider}`} className={ROW}>
              <td className={TD}>{usageName(row)}</td>
              <td className={TD}>{PROVIDER_NAMES[row.provider]}</td>
              <td className={TD}>{row.requests.toLocaleString()}</td>
              <td className={TD}>{tokens(row.inputTokens, row.outputTokens)}</td>
              <td className={TD}>
                {row.last30Days.requests.toLocaleString()} ({tokens(row.last30Days.inputTokens, row.last30Days.outputTokens)})
              </td>
              <td className={TD}>{row.lastAt ? timeAgo(row.lastAt) : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ApiKeys() {
  const keys = useApiKeys();
  const test = useTestKey();
  const remove = useDeleteKey();
  const [editing, setEditing] = useState<string | null>(null);
  const [result, setResult] = useState<KeyTest | null>(null);
  const data = keys.data;
  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">AI providers</h2>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Your own Anthropic or OpenAI API keys, for the assistant in the dashboard editor. A key is used only for your requests, and
        its use is counted below.
        {data?.workspaceKey && " You may also use the workspace's Claude key."}
        {data?.ollama && " Ollama is available too."}
      </p>
      <ErrorNote error={keys.error ?? test.error ?? remove.error} />
      {data && !data.enabled && (
        <p role="note" className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-900 dark:bg-amber-950/40">
          An admin has switched off personal API keys in this workspace. Your keys are kept, but not used, and new ones cannot be added.
        </p>
      )}
      {result && <TestResult result={result} />}
      {data && data.keys.length > 0 && (
        <div className="my-3 overflow-x-auto">
          <table className="w-full" aria-label="Your API keys">
            <thead>
              <tr>
                <th className={TH}>Name</th>
                <th className={TH}>Provider</th>
                <th className={TH}>Key</th>
                <th className={TH}>Default model</th>
                <th className={TH}>Test</th>
                <th className={TH}>Last used</th>
                <th className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.keys.map((apiKey) => (
                <tr key={apiKey.id} className={ROW}>
                  <td className={TD}>
                    <span className="font-medium">{apiKey.label}</span>
                    {editing === apiKey.id && <EditKey apiKey={apiKey} onClose={() => setEditing(null)} />}
                  </td>
                  <td className={TD}>{apiKey.providerName}</td>
                  <td className={`${TD} font-mono`}>ending {apiKey.last4}</td>
                  <td className={`${TD} font-mono`}>{apiKey.defaultModel ?? <span className="font-sans text-zinc-400">None</span>}</td>
                  <td className={TD}>
                    {apiKey.lastTestOk === null ? (
                      <Badge>Not tested</Badge>
                    ) : (
                      <span title={apiKey.lastTestMessage ?? undefined}>
                        <Badge tone={apiKey.lastTestOk ? "good" : "bad"}>{apiKey.lastTestOk ? "Works" : "Refused"}</Badge>
                      </span>
                    )}
                  </td>
                  <td className={TD}>{apiKey.lastUsedAt ? timeAgo(apiKey.lastUsedAt) : "Never"}</td>
                  <td className={`${TD} whitespace-nowrap text-right`}>
                    <Button
                      variant="ghost"
                      aria-label={`Test ${apiKey.label}`}
                      busy={test.isPending && test.variables === apiKey.id}
                      onClick={() => test.mutate(apiKey.id, { onSuccess: setResult })}
                    >
                      Test
                    </Button>
                    <Button variant="ghost" aria-label={`Edit ${apiKey.label}`} onClick={() => setEditing(editing === apiKey.id ? null : apiKey.id)}>
                      Edit
                    </Button>
                    <Button
                      variant="danger"
                      aria-label={`Delete ${apiKey.label}`}
                      busy={remove.isPending && remove.variables === apiKey.id}
                      onClick={() => remove.mutate(apiKey.id, { onSuccess: () => setResult(null) })}
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data?.enabled && (
        <div className="mt-4">
          <h3 className="mb-2 text-sm font-medium">Add a key</h3>
          <AddKey onTested={setResult} />
        </div>
      )}
      {data && (
        <div className="mt-5">
          <h3 className="mb-2 text-sm font-medium">Your assistant use</h3>
          <Usage rows={data.usage} />
        </div>
      )}
    </Card>
  );
}
