// Personal access tokens: how an AI client (Claude Code, Claude Desktop) signs in
// to the MCP server as you. A token is shown once, when it is made.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, Field, Input, PageHeader, Select, TD, TH } from "../components/ui";
import { apiDelete, apiGet, apiPost } from "../lib/api";
import { timeAgo } from "../lib/format";

interface Token {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  status: "active" | "expired" | "revoked";
  expiresAt: string;
  lastUsedAt: string | null;
  createdAt: string;
}

interface TokenList {
  tokens: Token[];
  scopes: string[];
  mcpAuth: "none" | "token";
  mcpUrl: string;
}

const SCOPE_HELP: Record<string, string> = {
  read: "Look at datasets, models and dashboards, and run queries",
  edit: "Create and change dashboards and models, refresh datasets",
  publish: "Publish dashboards to viewers",
  admin: "Reserved for administration tools",
};

const LIFETIMES = [
  [7, "7 days"],
  [30, "30 days"],
  [90, "90 days"],
  [365, "1 year"],
] as const;

export function Tokens() {
  const client = useQueryClient();
  const list = useQuery({ queryKey: ["tokens"], queryFn: () => apiGet<TokenList>("/api/tokens") });
  const refresh = () => void client.invalidateQueries({ queryKey: ["tokens"] });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read", "edit"]);
  const [days, setDays] = useState(90);
  const [created, setCreated] = useState<(Token & { token: string }) | null>(null);
  const [copied, setCopied] = useState(false);

  const create = useMutation({
    mutationFn: () => apiPost<Token & { token: string }>("/api/tokens", { name: name.trim(), scopes, days }),
    onSuccess: (made) => {
      setCreated(made);
      setCopied(false);
      setName("");
      refresh();
    },
  });
  const revoke = useMutation({ mutationFn: (id: string) => apiPost<Token>(`/api/tokens/${id}/revoke`), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => apiDelete(`/api/tokens/${id}`), onSuccess: refresh });

  const url = list.data?.mcpUrl ?? "";
  const toggle = (scope: string) => setScopes((now) => (now.includes(scope) ? now.filter((s) => s !== scope) : [...now, scope]));

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="Access tokens"
        description="A token lets an AI client such as Claude Code work in Opsis as you, through the MCP server. It can never do more than your own role allows."
      />
      <ErrorNote error={list.error ?? create.error ?? revoke.error ?? remove.error} />

      {list.data && (
        <Card className="mb-5 p-4 text-sm">
          <h2 className="mb-1 font-medium">Connecting a client</h2>
          <p className="text-zinc-600 dark:text-zinc-400">
            The MCP server is at <code className="rounded bg-zinc-100 px-1 py-0.5 dark:bg-zinc-800">{url}</code>.{" "}
            {list.data.mcpAuth === "none"
              ? "On this install it accepts local connections without a token, so a token is only needed if you turn MCP_AUTH=token on."
              : "It needs a token, sent as an Authorization header."}
          </p>
          <pre className="mt-2 overflow-x-auto rounded-md bg-zinc-100 p-2 font-mono text-xs dark:bg-zinc-950" aria-label="Command to add Opsis to Claude Code">
            {list.data.mcpAuth === "none"
              ? `claude mcp add --transport http opsis ${url}`
              : `claude mcp add --transport http opsis ${url} --header "Authorization: Bearer <your token>"`}
          </pre>
        </Card>
      )}

      {created && (
        <div role="alert" className="mb-5 rounded-lg border border-emerald-300 bg-emerald-50 p-4 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
          <p className="font-medium">Copy the token for "{created.name}" now. It will not be shown again.</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <code className="break-all rounded bg-white px-2 py-1 font-mono text-xs dark:bg-zinc-900" aria-label="New access token">
              {created.token}
            </code>
            <Button
              onClick={() =>
                void navigator.clipboard.writeText(created.token).then(
                  () => setCopied(true),
                  () => setCopied(false),
                )
              }
            >
              {copied ? "Copied" : "Copy"}
            </Button>
            <Button variant="ghost" onClick={() => setCreated(null)}>
              Done
            </Button>
          </div>
        </div>
      )}

      <Card className="mb-5 p-4">
        <h2 className="mb-3 font-medium">New token</h2>
        <form
          className="grid gap-3 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <Field label="Name" htmlFor="token-name" hint="Where it will be used, for example: Claude Code on my laptop." required>
            <Input id="token-name" value={name} maxLength={100} required onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Expires after" htmlFor="token-days">
            <Select id="token-days" value={days} onChange={(event) => setDays(Number(event.target.value))}>
              {LIFETIMES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          </Field>
          <fieldset className="sm:col-span-2">
            <legend className="mb-1 text-sm font-medium">What it may do</legend>
            <div className="grid gap-1 sm:grid-cols-2">
              {(list.data?.scopes ?? []).map((scope) => (
                <label key={scope} className="flex items-start gap-2 text-sm">
                  <input type="checkbox" className="mt-1" checked={scopes.includes(scope)} onChange={() => toggle(scope)} />
                  <span>
                    <span className="font-medium">{scope}</span>
                    <span className="block text-xs text-zinc-600 dark:text-zinc-400">{SCOPE_HELP[scope] ?? ""}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="sm:col-span-2">
            <Button type="submit" variant="primary" busy={create.isPending} disabled={!name.trim() || scopes.length === 0}>
              Create token
            </Button>
          </div>
        </form>
      </Card>

      <Card>
        {list.data?.tokens.length ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className={TH}>Name</th>
                  <th className={TH}>Token</th>
                  <th className={TH}>May</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>Last used</th>
                  <th className={TH}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.data.tokens.map((token) => (
                  <tr key={token.id} className="border-t border-zinc-200 dark:border-zinc-800">
                    <td className={TD}>{token.name}</td>
                    <td className={`${TD} font-mono text-xs`}>{token.prefix}...</td>
                    <td className={TD}>{token.scopes.join(", ")}</td>
                    <td className={TD}>
                      {token.status === "active" ? (
                        <Badge tone="good">Active until {new Date(token.expiresAt).toLocaleDateString()}</Badge>
                      ) : (
                        <Badge tone="bad">{token.status === "revoked" ? "Revoked" : "Expired"}</Badge>
                      )}
                    </td>
                    <td className={TD}>{token.lastUsedAt ? timeAgo(token.lastUsedAt) : "Never"}</td>
                    <td className={`${TD} text-right`}>
                      {token.status === "active" ? (
                        <Button busy={revoke.isPending && revoke.variables === token.id} onClick={() => revoke.mutate(token.id)} aria-label={`Revoke ${token.name}`}>
                          Revoke
                        </Button>
                      ) : (
                        <Button variant="ghost" busy={remove.isPending && remove.variables === token.id} onClick={() => remove.mutate(token.id)} aria-label={`Delete ${token.name}`}>
                          Delete
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No tokens yet.</Empty>
        )}
      </Card>
    </div>
  );
}
