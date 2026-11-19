// The audit log (admins): sign-ins, people, sharing, links, publishing and agent writes.
import { useState } from "react";
import { Button, Card, Empty, ErrorNote, Field, Input, PageHeader, ROW, Select, TD, TH } from "../components/ui";
import { type AuditEntry, useAudit } from "../lib/access";
import type { AppConfig } from "../lib/config";
import { formatDateTime } from "../lib/format";

const KINDS: [string, string][] = [
  ["", "Everything"],
  ["auth.", "Sign-in"],
  ["user.", "People"],
  ["group.", "Groups"],
  ["dashboard.", "Dashboards"],
  ["link.", "Share links"],
  ["agent.", "Assistant (MCP)"],
  ["token.", "Access tokens"],
  ["model.", "Semantic models"],
  ["dataset.", "Datasets"],
  ["connection.", "Connections"],
];

function who(entry: AuditEntry): string {
  if (entry.actorName) return entry.actorType === "agent" ? `Assistant for ${entry.actorName}` : entry.actorName;
  return entry.actorType === "system" ? "System" : "Unknown";
}

function details(entry: AuditEntry): string {
  const text = JSON.stringify(entry.detail);
  return text === "{}" ? "" : text.length > 240 ? `${text.slice(0, 240)}...` : text;
}

export function Audit({ config }: { config: AppConfig }) {
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [pages, setPages] = useState<(number | null)[]>([null]);
  const before = pages[pages.length - 1] ?? null;
  const audit = useAudit({ action, actor: actor.trim(), before });

  const reset = (update: () => void) => {
    update();
    setPages([null]);
  };

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title="Audit log" description="Who did what, newest first. Entries are never changed or removed." />
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label="Kind" htmlFor="audit-kind">
          <Select id="audit-kind" value={action} onChange={(e) => reset(() => setAction(e.target.value))}>
            {KINDS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Person" htmlFor="audit-actor" hint="Part of a name or email.">
          <Input id="audit-actor" value={actor} onChange={(e) => reset(() => setActor(e.target.value))} />
        </Field>
      </div>
      <ErrorNote error={audit.error} />
      <Card>
        {audit.data?.entries.length ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className={TH}>When</th>
                  <th className={TH}>Who</th>
                  <th className={TH}>What</th>
                  <th className={TH}>Details</th>
                </tr>
              </thead>
              <tbody>
                {audit.data.entries.map((entry) => (
                  <tr key={entry.id} className={ROW}>
                    <td className={`${TD} whitespace-nowrap`}>{formatDateTime(entry.ts, config.calendar.timezone)}</td>
                    <td className={TD}>
                      {who(entry)}
                      {entry.ip && <span className="block text-xs text-zinc-500">{entry.ip}</span>}
                    </td>
                    <td className={`${TD} font-mono text-xs`}>{entry.action}</td>
                    <td className={`${TD} max-w-md break-all font-mono text-xs text-zinc-600 dark:text-zinc-400`}>{details(entry)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>{audit.isPending ? "Loading..." : "Nothing matches."}</Empty>
        )}
      </Card>
      <div className="mt-3 flex gap-2">
        {pages.length > 1 && <Button onClick={() => setPages(pages.slice(0, -1))}>Newer</Button>}
        {audit.data?.next && <Button onClick={() => setPages([...pages, audit.data.next])}>Older</Button>}
      </div>
    </div>
  );
}
