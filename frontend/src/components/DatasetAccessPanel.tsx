// Who may use a dataset (sign-in modes). Unrestricted: everyone who builds.
// Restricted: admins, the dataset's owner, and the people and groups on its list.
// Its owner and admins manage this. A restriction an admin set is the admins' alone:
// its owner then sees it read-only (decided at the M7 review).
// Dashboards built on it stay viewable by those they are shared with, so the panel
// also lists dashboards that show its data outside the list.
import { useState } from "react";
import { Link } from "react-router-dom";
import { useDatasetAccess, useDirectory, useSetDatasetAccess } from "../lib/access";
import { Button, Card, ErrorNote } from "./ui";

export function DatasetAccessPanel({ datasetId }: { datasetId: string }) {
  const access = useDatasetAccess(datasetId, true);
  const directory = useDirectory();
  const save = useSetDatasetAccess(datasetId);
  const [draft, setDraft] = useState<{ restricted: boolean; grants: string[] } | null>(null);
  const data = access.data;
  const current = draft ?? (data ? { restricted: data.restricted, grants: data.grants.map((g) => g.grantee) } : null);
  if (!data || !current) return <ErrorNote error={access.error} />;
  // Only admins and the owner get here (DatasetDetail decides); the owner of an
  // admin-set restriction may look but not change.
  const locked = !data.canManage;
  if (locked && !(data.restricted && data.restrictedByAdmin)) return null;
  const toggle = (grantee: string) =>
    setDraft({
      ...current,
      grants: current.grants.includes(grantee) ? current.grants.filter((g) => g !== grantee) : [...current.grants, grantee],
    });
  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">Who may use this dataset</h2>
      <p className="mb-3 text-xs text-zinc-500">
        Owner: {data.owner?.name ?? "nobody (deleted)"}. The owner and admins manage this.
        {data.restricted && data.restrictedBy && ` Restricted by ${data.restrictedBy}${data.restrictedByAdmin ? " (an admin)" : ""}.`}
      </p>
      <label className="mb-3 flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={current.restricted}
          disabled={locked}
          onChange={(e) => setDraft({ ...current, restricted: e.target.checked })}
        />
        <span>
          Restricted: only admins, the owner, and the people and groups below may query it or build on it.
          {locked && (
            <span className="block text-xs text-amber-700 dark:text-amber-400">
              An admin restricted it, so only an admin can change who is on its list or lift the restriction.
            </span>
          )}
        </span>
      </label>
      {current.restricted && (
        <div className="mb-3 grid gap-1 sm:grid-cols-2">
          {(directory.data?.groups ?? []).map((group) => (
            <label key={group.id} className="flex items-center gap-2 text-sm">
              <input type="checkbox" disabled={locked} checked={current.grants.includes(`group:${group.id}`)} onChange={() => toggle(`group:${group.id}`)} />
              {group.name} <span className="text-xs text-zinc-500">(group)</span>
            </label>
          ))}
          {(directory.data?.users ?? [])
            .filter((user) => user.role !== "admin" && user.id !== data.owner?.id)
            .map((user) => (
              <label key={user.id} className="flex items-center gap-2 text-sm">
                <input type="checkbox" disabled={locked} checked={current.grants.includes(`user:${user.id}`)} onChange={() => toggle(`user:${user.id}`)} />
                {user.name} <span className="text-xs text-zinc-500">{user.email}</span>
              </label>
            ))}
        </div>
      )}
      {data.exposures.length > 0 && (
        <div role="note" className="mb-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950/40">
          <p className="mb-1 font-medium">Its data is visible outside the list through these dashboards:</p>
          <ul className="list-disc pl-5">
            {data.exposures.map((exposure) => (
              <li key={exposure.id}>
                <Link to={`/dashboards/${exposure.id}`} className="text-brand underline">
                  {exposure.title}
                </Link>
                {": "}
                {[...exposure.outside, ...(exposure.shareLinks ? [`${exposure.shareLinks} share link${exposure.shareLinks > 1 ? "s" : ""}`] : [])].join(", ")}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs">Change their sharing to close this. A restriction applies to direct use, not to dashboards shared earlier.</p>
        </div>
      )}
      <ErrorNote error={save.error ?? directory.error} />
      {!locked && (
        <Button variant="primary" disabled={draft === null} busy={save.isPending} onClick={() => draft && save.mutate(draft, { onSuccess: () => setDraft(null) })}>
          Save access
        </Button>
      )}
    </Card>
  );
}
