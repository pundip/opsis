// Who may use a dataset (admins, sign-in modes). Unrestricted: everyone who builds.
// Restricted: admins and the chosen people and groups. Dashboards built on it can
// still be viewed by those they are shared with.
import { useState } from "react";
import { useDatasetAccess, useDirectory, useSetDatasetAccess } from "../lib/access";
import { Button, Card, ErrorNote } from "./ui";

export function DatasetAccessPanel({ datasetId }: { datasetId: string }) {
  const access = useDatasetAccess(datasetId, true);
  const directory = useDirectory();
  const save = useSetDatasetAccess(datasetId);
  const [draft, setDraft] = useState<{ restricted: boolean; grants: string[] } | null>(null);
  const current = draft ?? (access.data ? { restricted: access.data.restricted, grants: access.data.grants.map((g) => g.grantee) } : null);
  if (!current) return <ErrorNote error={access.error} />;
  const toggle = (grantee: string) =>
    setDraft({
      ...current,
      grants: current.grants.includes(grantee) ? current.grants.filter((g) => g !== grantee) : [...current.grants, grantee],
    });
  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">Who may use this dataset</h2>
      <label className="mb-3 flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-1" checked={current.restricted} onChange={(e) => setDraft({ ...current, restricted: e.target.checked })} />
        <span>
          Restricted: only admins and the people and groups below may query it or build on it.
          <span className="block text-xs text-zinc-500">
            Dashboards already built on it stay viewable by those they are shared with, with row-level security applied.
          </span>
        </span>
      </label>
      {current.restricted && (
        <div className="mb-3 grid gap-1 sm:grid-cols-2">
          {(directory.data?.groups ?? []).map((group) => (
            <label key={group.id} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={current.grants.includes(`group:${group.id}`)} onChange={() => toggle(`group:${group.id}`)} />
              {group.name} <span className="text-xs text-zinc-500">(group)</span>
            </label>
          ))}
          {(directory.data?.users ?? [])
            .filter((user) => user.role !== "admin")
            .map((user) => (
              <label key={user.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={current.grants.includes(`user:${user.id}`)} onChange={() => toggle(`user:${user.id}`)} />
                {user.name} <span className="text-xs text-zinc-500">{user.email}</span>
              </label>
            ))}
        </div>
      )}
      <ErrorNote error={save.error ?? directory.error} />
      <Button variant="primary" disabled={draft === null} busy={save.isPending} onClick={() => draft && save.mutate(draft, { onSuccess: () => setDraft(null) })}>
        Save access
      </Button>
    </Card>
  );
}
