import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { DatasetAccessPanel } from "../components/DatasetAccessPanel";
import type { AppConfig, Me } from "../lib/config";
import {
  type Job,
  isActive,
  useDataset,
  useDeleteDataset,
  useJobs,
  usePreview,
  useRefreshDataset,
  useSnapshots,
} from "../lib/data";
import {
  formatBytes, formatCell, formatDateTime, formatDuration, formatNumber, timeAgo,
} from "../lib/format";
import {
  Badge, Button, Card, Empty, ErrorNote, PageHeader, ROW, StatusBadge, TD, TH,
} from "../components/ui";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-zinc-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium">{children}</dd>
    </div>
  );
}

export function JobRow({ job, timezone, name }: { job: Job; timezone: string; name?: React.ReactNode }) {
  return (
    <tr className={ROW}>
      {name !== undefined && <td className={TD}>{name}</td>}
      <td className={TD}>
        <StatusBadge status={job.status} />
        {job.full && <span className="ml-1 text-xs text-zinc-500">full rebuild</span>}
      </td>
      <td className={TD}>
        <span title={timeAgo(job.createdAt)}>{formatDateTime(job.createdAt, timezone)}</span>
      </td>
      <td className={`${TD} tabular-nums`}>
        {job.result?.seconds !== undefined ? formatDuration(job.result.seconds) : ""}
      </td>
      <td className={`${TD} text-right tabular-nums`}>
        {job.result?.rowCount !== undefined ? formatNumber(job.result.rowCount) : ""}
      </td>
      <td className={`${TD} max-w-sm text-xs text-red-700 dark:text-red-400`}>{job.error}</td>
    </tr>
  );
}

export function DatasetDetail({ config, me }: { config: AppConfig; me: Me }) {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const dataset = useDataset(id);
  const snapshotId = dataset.data?.snapshot?.id;
  const preview = usePreview(id, snapshotId);
  const snapshots = useSnapshots(id, snapshotId);
  const jobs = useJobs(id);
  const refresh = useRefreshDataset();
  const remove = useDeleteDataset();
  const timezone = config.calendar.timezone;
  const lastStatus = dataset.data?.lastJob?.status;

  // When a refresh finishes, reload the job history too.
  useEffect(() => {
    void client.invalidateQueries({ queryKey: ["jobs", id] });
  }, [client, id, lastStatus]);

  if (dataset.isPending) return <p className="text-sm text-zinc-500">Loading...</p>;
  if (dataset.isError) {
    return (
      <div className="mx-auto max-w-5xl">
        <ErrorNote error={dataset.error} />
        <Link to="/datasets" className="mt-3 inline-block text-sm text-brand underline">
          Back to datasets
        </Link>
      </div>
    );
  }
  const data = dataset.data;
  const busy = isActive(data.lastJob);

  return (
    <div className="mx-auto max-w-6xl">
      <p className="mb-2 text-sm">
        <Link to="/datasets" className="text-brand underline">
          Datasets
        </Link>
        <span className="text-zinc-500"> / {data.name}</span>
      </p>
      <PageHeader
        title={data.label}
        description={
          <>
            <span className="font-mono">{data.name}</span> from {data.connectionName},{" "}
            {data.stream}
          </>
        }
        actions={
          <>
            <Button variant="primary" busy={busy} onClick={() => refresh.mutate({ id })}>
              {busy ? "Refreshing" : "Refresh now"}
            </Button>
            {data.refreshMode !== "full" && (
              <Button
                disabled={busy}
                title="Reload every row from the source and reset the cursor"
                onClick={() => refresh.mutate({ id, full: true })}
              >
                Full rebuild
              </Button>
            )}
            <Button
              variant="danger"
              onClick={() => {
                if (window.confirm(`Delete the dataset "${data.label}" and its stored data?`)) {
                  remove.mutate(id, { onSuccess: () => navigate("/datasets") });
                }
              }}
            >
              Delete
            </Button>
          </>
        }
      />
      <ErrorNote error={refresh.error ?? remove.error} />
      {config.authMode !== "none" && (me.permissions.includes("settings:manage") || data.createdBy === me.id) && (
        <DatasetAccessPanel datasetId={id} />
      )}

      <Card className="mb-5 p-4">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-5">
          <Fact label="Status">
            {data.lastJob ? <StatusBadge status={data.lastJob.status} /> : "Never refreshed"}
          </Fact>
          <Fact label="Rows">{data.snapshot ? formatNumber(data.snapshot.rowCount) : "None"}</Fact>
          <Fact label="Stored size">
            {data.snapshot ? formatBytes(data.snapshot.sizeBytes) : "None"}
          </Fact>
          <Fact label="Data as of">
            {data.snapshot ? (
              <span title={formatDateTime(data.snapshot.createdAt, timezone)}>
                {timeAgo(data.snapshot.createdAt)}
              </span>
            ) : (
              "No data yet"
            )}
          </Fact>
          <Fact label="Refresh mode">
            {data.refreshMode}
            {data.cursorField && (
              <span className="block text-xs font-normal text-zinc-500">
                cursor: {data.cursorField}
                {data.primaryKey.length > 0 && `, key: ${data.primaryKey.join(", ")}`}
              </span>
            )}
          </Fact>
        </dl>
        {data.lastJob?.status === "failed" && (
          <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-400">
            The last refresh failed: {data.lastJob.error}
            {data.snapshot && " The data below is from the last successful refresh."}
          </p>
        )}
      </Card>

      <h2 className="mb-2 text-base font-semibold">Preview</h2>
      <Card className="mb-5">
        {!data.snapshot ? (
          <Empty>{busy ? "The first refresh is running." : "No data yet. Run a refresh."}</Empty>
        ) : preview.isError ? (
          <div className="p-4">
            <ErrorNote error={preview.error} />
          </div>
        ) : preview.data ? (
          <>
            <div className="max-h-[28rem] overflow-auto">
              <table className="w-full border-separate border-spacing-0 text-sm">
                <thead>
                  <tr>
                    {preview.data.columns.map((column) => (
                      <th
                        key={column.name}
                        scope="col"
                        className="sticky top-0 border-b border-zinc-200 bg-zinc-50 px-3 py-2 text-left whitespace-nowrap dark:border-zinc-800 dark:bg-zinc-900"
                      >
                        <span className="font-medium">{column.name}</span>
                        <span className="block text-xs font-normal text-zinc-500">
                          {column.type}
                        </span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data.rows.map((row, index) => (
                    <tr key={index} className="odd:bg-zinc-50/60 dark:odd:bg-zinc-800/30">
                      {row.map((cell, cellIndex) => {
                        const type = preview.data.columns[cellIndex]?.type;
                        const numeric = type === "integer" || type === "float" || type === "decimal";
                        return (
                          <td
                            key={cellIndex}
                            className={`max-w-xs truncate border-b border-zinc-100 px-3 py-1.5 whitespace-nowrap dark:border-zinc-800 ${numeric ? "text-right tabular-nums" : ""}`}
                            title={cell === null ? undefined : formatCell(cell, type)}
                          >
                            {cell === null ? (
                              <span className="text-zinc-400 italic">null</span>
                            ) : (
                              formatCell(cell, type)
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="border-t border-zinc-200 px-3 py-2 text-xs text-zinc-500 dark:border-zinc-800">
              {preview.data.rowCount === null
                ? `Showing ${formatNumber(preview.data.rows.length)} rows, ${preview.data.columns.length} columns. Row-level security applies to you: only rows you may see are shown.`
                : `Showing ${formatNumber(preview.data.rows.length)} of ${formatNumber(preview.data.rowCount)} rows, ${preview.data.columns.length} columns.`}
            </p>
          </>
        ) : (
          <Empty>Loading preview...</Empty>
        )}
      </Card>

      <div className="grid gap-5 lg:grid-cols-5">
        <div className="lg:col-span-3">
          <h2 className="mb-2 text-base font-semibold">Refresh history</h2>
          <Card>
            {jobs.data?.length ? (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr>
                      <th className={TH}>Status</th>
                      <th className={TH}>Requested</th>
                      <th className={TH}>Took</th>
                      <th className={`${TH} text-right`}>Rows</th>
                      <th className={TH}>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.data.map((job) => (
                      <JobRow key={job.id} job={job} timezone={timezone} />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty>No refreshes yet.</Empty>
            )}
          </Card>
        </div>
        <div className="lg:col-span-2">
          <h2 className="mb-2 text-base font-semibold">Snapshots kept</h2>
          <Card>
            {snapshots.data?.length ? (
              <table className="w-full">
                <thead>
                  <tr>
                    <th className={TH}>Created</th>
                    <th className={`${TH} text-right`}>Rows</th>
                    <th className={`${TH} text-right`}>Size</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.data.map((snapshot) => (
                    <tr key={snapshot.id} className={ROW}>
                      <td className={TD}>
                        {formatDateTime(snapshot.createdAt, timezone)}{" "}
                        {snapshot.current && <Badge tone="good">Current</Badge>}
                        <span className="block text-xs text-zinc-500">{snapshot.mode}</span>
                      </td>
                      <td className={`${TD} text-right tabular-nums`}>
                        {formatNumber(snapshot.rowCount)}
                      </td>
                      <td className={`${TD} text-right tabular-nums`}>
                        {formatBytes(snapshot.sizeBytes)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <Empty>None yet.</Empty>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
