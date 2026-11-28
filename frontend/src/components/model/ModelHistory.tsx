// The model's versions: who saved each and why, what changed from the one before,
// and restoring one (which saves it as a new version).
import { useState } from "react";
import { timeAgo } from "../../lib/format";
import { type VersionEntry, useModelDiff, useModelVersions, useRestoreVersion } from "../../lib/workspaceModel";
import { Badge, Button, Card, ErrorNote } from "../ui";
import { SaveError } from "./lists";

function Changes({ version }: { version: VersionEntry }) {
  const diff = useModelDiff(version.version - 1, version.version);
  if (diff.isPending) return <p className="text-xs text-zinc-500">Reading the changes.</p>;
  if (diff.error) return <ErrorNote error={diff.error} />;
  const { tables, relationships, ops } = diff.data;
  return (
    <div className="mt-2 space-y-1 rounded-md bg-zinc-50 p-2 text-xs dark:bg-zinc-950">
      {tables.length > 0 && <p>Tables: {tables.join(", ")}</p>}
      {relationships.length > 0 && <p>Relationships: {relationships.join(", ")}</p>}
      {ops.length === 0 && <p>No changes you can see.</p>}
      {ops.length > 0 && (
        <ul className="max-h-48 overflow-auto font-mono">
          {ops.map((op, index) => (
            <li key={index}>
              {op.op} {op.path}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ModelHistory() {
  const versions = useModelVersions(true);
  const restore = useRestoreVersion();
  const [open, setOpen] = useState<number | null>(null);
  return (
    <Card className="p-4">
      <h2 className="mb-3 text-base font-semibold">Version history</h2>
      <SaveError error={restore.error} />
      <ErrorNote error={versions.error} />
      <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
        {versions.data?.map((version) => (
          <li key={version.version} className="py-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Version {version.version}</span>
              {version.current && <Badge tone="good">Current</Badge>}
              <span className="text-zinc-600 dark:text-zinc-400">
                {version.authorName}, {timeAgo(version.createdAt)}
                {version.message && `: ${version.message}`}
              </span>
              <span className="ml-auto flex gap-1">
                <Button
                  variant="ghost"
                  aria-expanded={open === version.version}
                  onClick={() => setOpen(open === version.version ? null : version.version)}
                >
                  Changes
                </Button>
                {!version.current && (
                  <Button
                    variant="ghost"
                    busy={restore.isPending && restore.variables === version.version}
                    onClick={() => {
                      if (window.confirm(`Restore version ${version.version}? It becomes a new version.`)) {
                        restore.mutate(version.version);
                      }
                    }}
                  >
                    Restore
                  </Button>
                )}
              </span>
            </div>
            {open === version.version && <Changes version={version} />}
          </li>
        ))}
        {versions.data?.length === 0 && <li className="py-2 text-sm text-zinc-500">No saved versions yet.</li>}
      </ul>
    </Card>
  );
}
