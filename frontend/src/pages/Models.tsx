import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { type ModelFileResult, useImportModels, useModels } from "../lib/semantic";
import type { Me } from "../lib/config";
import { timeAgo } from "../lib/format";
import { url } from "../lib/runtime";
import { Badge, Button, Card, Empty, ErrorNote, PageHeader, ROW, TD, TH } from "../components/ui";

const RESULT: Record<ModelFileResult["status"], { label: string; tone: "good" | "bad" | "neutral" | "busy" }> = {
  changed: { label: "Will change", tone: "busy" },
  unchanged: { label: "No change", tone: "neutral" },
  invalid: { label: "Not valid", tone: "bad" },
  unknown_dataset: { label: "No such dataset", tone: "bad" },
  saved: { label: "Saved", tone: "good" },
};

/** A models/ folder in: every file is checked first, and saved only if all are valid. */
function ImportModels({ onClose }: { onClose: () => void }) {
  const run = useImportModels();
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const report = run.data;
  const changes = report?.files.filter((f) => f.status === "changed").length ?? 0;
  return (
    <Card className="mb-5 p-4">
      <div className="mb-1 flex items-center gap-2">
        <h2 className="mr-auto font-medium">Import models</h2>
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        A zip of a <code>models/</code> folder, as Export all makes, or <code>.yml</code> files. Every file is checked
        first, and nothing is saved unless all of them are valid. Each model that changes gets a new version, so it can be
        restored.
      </p>
      <input
        ref={input}
        type="file"
        multiple
        accept=".zip,.yml,.yaml"
        aria-label="Model files"
        className="text-sm"
        onChange={(event) => {
          const chosen = Array.from(event.target.files ?? []);
          setFiles(chosen);
          if (chosen.length > 0) run.mutate({ files: chosen, dryRun: true });
        }}
      />
      <ErrorNote error={run.error} />
      {report && (
        <>
          <table className="mt-3 w-full">
            <thead>
              <tr>
                <th className={TH}>File</th>
                <th className={TH}>Dataset</th>
                <th className={TH}>Result</th>
              </tr>
            </thead>
            <tbody>
              {report.files.map((file) => (
                <tr key={file.file} className={ROW}>
                  <td className={`${TD} font-mono text-xs`}>{file.file}</td>
                  <td className={TD}>{file.dataset ?? ""}</td>
                  <td className={TD}>
                    <Badge tone={RESULT[file.status].tone}>
                      {file.status === "saved" && file.version ? `Saved as version ${file.version}` : RESULT[file.status].label}
                    </Badge>
                    {(file.problems ?? []).map((problem) => (
                      <span key={`${problem.path}-${problem.message}`} className="block text-xs text-red-700 dark:text-red-400">
                        {problem.path !== "/" ? `${problem.path}: ` : ""}
                        {problem.message}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p role="status" className="mt-3 text-sm">
            {report.applied
              ? "Imported."
              : !report.valid
                ? "Nothing was saved: fix the files marked Not valid or No such dataset, then choose them again."
                : changes === 0
                  ? "Every model is already the same. Nothing to save."
                  : `${changes} model${changes === 1 ? "" : "s"} will change.`}
          </p>
          {!report.applied && report.valid && changes > 0 && (
            <Button className="mt-2" variant="primary" busy={run.isPending} onClick={() => run.mutate({ files, dryRun: false })}>
              Save {changes} model{changes === 1 ? "" : "s"}
            </Button>
          )}
        </>
      )}
    </Card>
  );
}

export function Models({ me }: { me: Me }) {
  const models = useModels();
  const [importing, setImporting] = useState(false);
  const canEdit = me.permissions.includes("models:edit");
  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Semantic models"
        description="Each dataset has a model that names its dimensions and measures once, so every dashboard and the assistant use the same definitions."
        actions={
          <>
            <a
              href={url("/api/model-files")}
              download
              className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
            >
              Export all
            </a>
            {canEdit && (
              <Button aria-expanded={importing} onClick={() => setImporting(!importing)}>
                Import
              </Button>
            )}
          </>
        }
      />
      {importing && <ImportModels onClose={() => setImporting(false)} />}
      <ErrorNote error={models.error} />
      <Card>
        {models.data?.length === 0 ? (
          <Empty>
            No datasets yet.{" "}
            <Link to="/datasets" className="text-brand underline">
              Create a dataset
            </Link>{" "}
            and its model appears here.
          </Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Dataset</th>
                <th className={`${TH} text-right`}>Dimensions</th>
                <th className={`${TH} text-right`}>Measures</th>
                <th className={TH}>Joins</th>
                <th className={TH}>Row-level security</th>
                <th className={TH}>Status</th>
              </tr>
            </thead>
            <tbody>
              {models.data?.map((model) => (
                <tr key={model.dataset} className={ROW}>
                  <td className={TD}>
                    <Link to={`/models/${model.dataset}`} className="font-medium text-brand underline">
                      {model.label}
                    </Link>
                    <span className="block font-mono text-xs text-zinc-500">{model.dataset}</span>
                  </td>
                  <td className={`${TD} text-right tabular-nums`}>{model.dimensions}</td>
                  <td className={`${TD} text-right tabular-nums`}>{model.measures}</td>
                  <td className={TD}>{model.joins.join(", ")}</td>
                  <td className={TD}>
                    {model.rlsRules > 0
                      ? `${model.rlsRules} rule${model.rlsRules === 1 ? "" : "s"}`
                      : "None"}
                  </td>
                  <td className={TD}>
                    {!model.hasData ? (
                      <Badge>No data yet</Badge>
                    ) : model.isStarter ? (
                      <Badge>Generated</Badge>
                    ) : (
                      <>
                        <Badge tone="good">Edited</Badge>{" "}
                        {model.updatedAt && (
                          <span className="text-xs text-zinc-500">{timeAgo(model.updatedAt)}</span>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
