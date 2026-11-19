// Who can see or change a dashboard, and its public share links.
import { useMemo, useState } from "react";
import {
  type Share,
  useCreateShareLink,
  useDashboardAccess,
  useDirectory,
  useRevokeShareLink,
  useSetDashboardAccess,
  useShareLinks,
} from "../../lib/access";
import type { AppConfig, Me } from "../../lib/config";
import { timeAgo } from "../../lib/format";
import { OneTimeLink } from "../../pages/Users";
import { Badge, Button, Card, ErrorNote, Field, Input, ROW, Select, TD, TH } from "../ui";

const EXPIRY: [string, string][] = [
  ["", "Never"],
  ["1", "1 day"],
  ["7", "7 days"],
  ["30", "30 days"],
  ["90", "90 days"],
];

function People({ dashboardId }: { dashboardId: string }) {
  const access = useDashboardAccess(dashboardId);
  const directory = useDirectory();
  const save = useSetDashboardAccess(dashboardId);
  const [draft, setDraft] = useState<Share[] | null>(null);
  const [adding, setAdding] = useState("");
  const [role, setRole] = useState<"viewer" | "editor">("viewer");
  const shares = useMemo(() => draft ?? access.data?.shares ?? [], [draft, access.data]);
  const options = useMemo(() => {
    const taken = new Set(shares.map((s) => s.grantee));
    const list: { grantee: string; name: string; kind: Share["kind"]; email?: string }[] = [];
    if (!taken.has("everyone")) list.push({ grantee: "everyone", name: "Everyone in the workspace", kind: "everyone" });
    for (const group of directory.data?.groups ?? []) {
      if (!taken.has(`group:${group.id}`)) list.push({ grantee: `group:${group.id}`, name: `${group.name} (group)`, kind: "group" });
    }
    for (const user of directory.data?.users ?? []) {
      if (!taken.has(`user:${user.id}`) && user.id !== access.data?.owner?.id) {
        list.push({ grantee: `user:${user.id}`, name: `${user.name} (${user.email})`, kind: "user", email: user.email });
      }
    }
    return list;
  }, [directory.data, shares, access.data?.owner?.id]);

  return (
    <div>
      <p className="mb-2 text-sm">
        Owner: <strong>{access.data?.owner?.name ?? "nobody"}</strong>. Admins can always see and change it.
      </p>
      <ul className="mb-3 divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {shares.length === 0 && <li className="px-3 py-2 text-sm text-zinc-500">Not shared with anyone yet.</li>}
        {shares.map((share) => (
          <li key={share.grantee} className="flex items-center gap-2 px-3 py-2 text-sm">
            <span className="mr-auto">
              {share.name}
              {share.email && <span className="ml-1 text-xs text-zinc-500">{share.email}</span>}
            </span>
            <Select
              aria-label={`Role for ${share.name}`}
              className="!w-28"
              value={share.role}
              onChange={(e) => setDraft(shares.map((s) => (s.grantee === share.grantee ? { ...s, role: e.target.value as Share["role"] } : s)))}
            >
              <option value="viewer">Can view</option>
              <option value="editor">Can edit</option>
            </Select>
            <Button variant="ghost" aria-label={`Stop sharing with ${share.name}`} onClick={() => setDraft(shares.filter((s) => s.grantee !== share.grantee))}>
              Remove
            </Button>
          </li>
        ))}
      </ul>
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <Field label="Share with" htmlFor="share-with">
          <Select id="share-with" className="!w-72" value={adding} onChange={(e) => setAdding(e.target.value)}>
            <option value="">Choose a person or group...</option>
            {options.map((o) => (
              <option key={o.grantee} value={o.grantee}>
                {o.name}
              </option>
            ))}
          </Select>
        </Field>
        <Select aria-label="Role for the new share" className="!w-28" value={role} onChange={(e) => setRole(e.target.value as "viewer" | "editor")}>
          <option value="viewer">Can view</option>
          <option value="editor">Can edit</option>
        </Select>
        <Button
          disabled={!adding}
          onClick={() => {
            const option = options.find((o) => o.grantee === adding);
            if (!option) return;
            setDraft([...shares, { grantee: option.grantee, role, name: option.name, kind: option.kind, email: option.email }]);
            setAdding("");
          }}
        >
          Add
        </Button>
      </div>
      <ErrorNote error={access.error ?? directory.error ?? save.error} />
      <Button
        variant="primary"
        disabled={draft === null}
        busy={save.isPending}
        onClick={() => save.mutate(shares.map(({ grantee, role: r }) => ({ grantee, role: r })), { onSuccess: () => setDraft(null) })}
      >
        Save sharing
      </Button>
      {draft !== null && <span className="ml-2 text-xs text-zinc-500">Not saved yet.</span>}
    </div>
  );
}

function Links({ dashboardId, me, published }: { dashboardId: string; me: Me; published: boolean }) {
  const links = useShareLinks(dashboardId, true);
  const create = useCreateShareLink(dashboardId);
  const revoke = useRevokeShareLink(dashboardId);
  const admin = me.permissions.includes("settings:manage");
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("30");
  const [password, setPassword] = useState("");
  const [rls, setRls] = useState<"none" | "mine" | "custom">("none");
  const [custom, setCustom] = useState("{}");
  const [made, setMade] = useState<string | null>(null);
  let customValue: Record<string, unknown> | null = null;
  try {
    const parsed: unknown = JSON.parse(custom || "{}");
    customValue = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
  } catch {
    customValue = null;
  }

  return (
    <div>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Anyone with a link can view the published version without signing in.
        {!published && " Publish the dashboard before making a link."}
      </p>
      {made && <OneTimeLink label="Share link" link={made} onDone={() => setMade(null)} />}
      <form
        className="mb-4 grid gap-3 sm:grid-cols-2"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            {
              name: name.trim() || undefined,
              expiresInDays: expiry ? Number(expiry) : null,
              password: password || undefined,
              attributes: rls === "mine" ? "mine" : rls === "custom" ? customValue : null,
            },
            {
              onSuccess: (link) => {
                setMade(link.url);
                setName("");
                setPassword("");
              },
            },
          );
        }}
      >
        <Field label="Name" htmlFor="link-name" hint="Who it is for, for example: Board members.">
          <Input id="link-name" maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Expires after" htmlFor="link-expiry">
          <Select id="link-expiry" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
            {EXPIRY.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Password (optional)" htmlFor="link-password" hint="At least 12 characters.">
          <Input id="link-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Data protected by row-level security" htmlFor="link-rls">
          <Select id="link-rls" value={rls} onChange={(e) => setRls(e.target.value as typeof rls)}>
            <option value="none">Show none of it (safest)</option>
            <option value="mine">Show what I can see</option>
            {admin && <option value="custom">Choose attributes...</option>}
          </Select>
        </Field>
        {rls === "custom" && (
          <Field label="Attributes" htmlFor="link-attributes" hint='JSON, for example {"regions": ["VIC"]}' error={customValue === null ? "Not a JSON object." : undefined}>
            <Input id="link-attributes" className="font-mono" value={custom} onChange={(e) => setCustom(e.target.value)} />
          </Field>
        )}
        <div className="sm:col-span-2">
          <Button type="submit" variant="primary" busy={create.isPending} disabled={!published || (rls === "custom" && customValue === null)}>
            Create link
          </Button>
        </div>
      </form>
      <ErrorNote error={links.error ?? create.error ?? revoke.error} />
      {links.data && links.data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Link</th>
                <th className={TH}>Status</th>
                <th className={TH}>Views</th>
                <th className={TH}>Expires</th>
                <th className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {links.data.map((link) => (
                <tr key={link.id} className={ROW}>
                  <td className={TD}>
                    {link.name ?? <span className="font-mono text-xs">{link.prefix}...</span>}
                    {link.hasPassword && <span className="ml-1 text-xs text-zinc-500">(password)</span>}
                    <span className="block text-xs text-zinc-500">
                      by {link.createdBy ?? "someone"}, {timeAgo(link.createdAt)}
                    </span>
                  </td>
                  <td className={TD}>
                    <Badge tone={link.status === "active" ? "good" : "bad"}>{link.status}</Badge>
                  </td>
                  <td className={TD}>{link.viewCount}</td>
                  <td className={TD}>{link.expiresAt ? new Date(link.expiresAt).toLocaleDateString() : "Never"}</td>
                  <td className={`${TD} text-right`}>
                    {link.status === "active" && (
                      <Button variant="danger" aria-label={`Revoke link ${link.name ?? link.prefix}`} busy={revoke.isPending && revoke.variables === link.id} onClick={() => revoke.mutate(link.id)}>
                        Revoke
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function SharePanel({
  dashboardId,
  config,
  me,
  published,
  canPublish,
}: {
  dashboardId: string;
  config: AppConfig;
  me: Me;
  published: boolean;
  canPublish: boolean;
}) {
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-medium">Share</h2>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Viewers see the published version only. Editors can change the draft.
      </p>
      <People dashboardId={dashboardId} />
      {config.features.publicShareLinks && canPublish && (
        <div className="mt-5 border-t border-zinc-200 pt-4 dark:border-zinc-800">
          <h3 className="mb-1 font-medium">Public links</h3>
          <Links dashboardId={dashboardId} me={me} published={published} />
        </div>
      )}
    </Card>
  );
}
