import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "../lib/api";
import {
  type Connection,
  type RefreshMode,
  type Stream,
  isActive,
  useConnections,
  useCreateDataset,
  useDatasets,
  useRefreshDataset,
  useStreams,
} from "../lib/data";
import { formatNumber, timeAgo } from "../lib/format";
import {
  Button, Card, Empty, ErrorNote, Field, Input, PageHeader, ROW, Select, StatusBadge, TD, TH,
} from "../components/ui";

const MODE_LABELS: Record<RefreshMode, string> = {
  full: "Full: replace everything each time",
  append: "Append: add rows newer than the cursor",
  merge: "Merge: add new rows and update changed ones",
};

function NewDataset({ connections, onDone }: { connections: Connection[]; onDone: () => void }) {
  const navigate = useNavigate();
  const [connectionId, setConnectionId] = useState(connections[0]?.id ?? "");
  const streams = useStreams(connectionId || null);
  const [streamName, setStreamName] = useState("");
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState<RefreshMode>("full");
  const [cursor, setCursor] = useState("");
  const create = useCreateDataset();
  const stream = streams.data?.find((s) => s.name === streamName);
  const details = create.error instanceof ApiError ? create.error.details : [];
  const problem = (path: string) => details.find((d) => d.path === path)?.message;

  function pick(next: Stream | undefined) {
    setStreamName(next?.name ?? "");
    setName(next?.suggestedName ?? "");
    setLabel(next?.label ?? "");
    setMode("full");
    setCursor(next?.cursorCandidates[0] ?? "");
    create.reset();
  }

  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-4 text-base font-semibold">New dataset</h2>
      <form
        className="grid max-w-xl gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            {
              name,
              label,
              connectionId,
              stream: streamName,
              refreshMode: mode,
              cursorField: mode === "full" ? null : cursor,
              primaryKey: [],
            },
            { onSuccess: (dataset) => navigate(`/datasets/${dataset.id}`) },
          );
        }}
      >
        <Field label="Connection" htmlFor="ds-connection">
          <Select
            id="ds-connection"
            value={connectionId}
            onChange={(event) => {
              setConnectionId(event.target.value);
              pick(undefined);
            }}
          >
            {connections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.connectorName})
              </option>
            ))}
          </Select>
        </Field>
        <ErrorNote error={streams.error} />
        <Field
          label="Table"
          htmlFor="ds-stream"
          required
          hint={streams.isFetching ? "Reading the list of tables..." : undefined}
        >
          <Select
            id="ds-stream"
            value={streamName}
            required
            disabled={!streams.data}
            onChange={(e) => pick(streams.data?.find((s) => s.name === e.target.value))}
          >
            <option value="">Choose a table</option>
            {streams.data?.map((s) => (
              <option key={s.name} value={s.name}>
                {s.label}
                {s.rowCountEstimate !== null ? ` (about ${formatNumber(s.rowCountEstimate)} rows)` : ""}
              </option>
            ))}
          </Select>
        </Field>
        {stream && (
          <>
            <p className="text-xs text-zinc-500">
              {stream.columns.length} columns: {stream.columns.map((c) => c.name).join(", ")}
            </p>
            <Field
              label="Dataset name"
              htmlFor="ds-name"
              required
              error={problem("/name")}
              hint="Used in queries. Lower-case letters, digits and underscores."
            >
              <Input
                id="ds-name"
                value={name}
                required
                pattern="[a-z_][a-z0-9_]{0,62}"
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label="Display name" htmlFor="ds-label">
              <Input id="ds-label" value={label} onChange={(e) => setLabel(e.target.value)} />
            </Field>
            <Field label="Refresh mode" htmlFor="ds-mode" error={problem("/refreshMode")}>
              <Select id="ds-mode" value={mode} onChange={(e) => setMode(e.target.value as RefreshMode)}>
                {stream.supportedModes.map((m) => (
                  <option key={m} value={m}>
                    {MODE_LABELS[m]}
                  </option>
                ))}
              </Select>
            </Field>
            {mode !== "full" && (
              <Field
                label="Cursor column"
                htmlFor="ds-cursor"
                error={problem("/cursorField")}
                hint={
                  mode === "merge"
                    ? `A column that increases whenever a row changes. Rows are matched on ${stream.primaryKey.join(", ")}.`
                    : "A column that only increases, such as an id or a created date."
                }
              >
                <Select id="ds-cursor" value={cursor} onChange={(e) => setCursor(e.target.value)}>
                  {stream.cursorCandidates.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
          </>
        )}
        <ErrorNote error={details.length ? null : create.error} />
        <div className="flex gap-2">
          <Button type="submit" variant="primary" busy={create.isPending} disabled={!stream}>
            Create and refresh
          </Button>
          <Button variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function Datasets() {
  const datasets = useDatasets();
  const connections = useConnections();
  const refresh = useRefreshDataset();
  const [creating, setCreating] = useState(false);
  const hasConnections = (connections.data?.length ?? 0) > 0;

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Datasets"
        description="Tables pulled from your connections into local storage. Dashboards read these copies, never the source."
        actions={
          !creating && (
            <Button variant="primary" disabled={!hasConnections} onClick={() => setCreating(true)}>
              New dataset
            </Button>
          )
        }
      />
      {creating && connections.data && (
        <NewDataset connections={connections.data} onDone={() => setCreating(false)} />
      )}
      <ErrorNote error={datasets.error ?? refresh.error} />
      <Card>
        {datasets.data?.length === 0 ? (
          <Empty>
            {hasConnections ? (
              "No datasets yet. Create one from a table in a connection."
            ) : (
              <>
                No datasets yet. <Link to="/connections" className="text-brand underline">Add a connection</Link> first.
              </>
            )}
          </Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Dataset</th>
                <th className={TH}>Source</th>
                <th className={TH}>Mode</th>
                <th className={`${TH} text-right`}>Rows</th>
                <th className={TH}>Last refresh</th>
                <th className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {datasets.data?.map((dataset) => (
                <tr key={dataset.id} className={ROW}>
                  <td className={TD}>
                    <Link to={`/datasets/${dataset.id}`} className="font-medium text-brand underline">
                      {dataset.label}
                    </Link>
                    <span className="block font-mono text-xs text-zinc-500">{dataset.name}</span>
                  </td>
                  <td className={TD}>
                    {dataset.connectionName}
                    <span className="block text-xs text-zinc-500">{dataset.stream}</span>
                  </td>
                  <td className={TD}>{dataset.refreshMode}</td>
                  <td className={`${TD} text-right tabular-nums`}>
                    {dataset.snapshot ? formatNumber(dataset.snapshot.rowCount) : ""}
                  </td>
                  <td className={TD}>
                    {dataset.lastJob && <StatusBadge status={dataset.lastJob.status} />}{" "}
                    {dataset.snapshot && (
                      <span className="text-xs text-zinc-500">
                        data from {timeAgo(dataset.snapshot.createdAt)}
                      </span>
                    )}
                    {dataset.lastJob?.status === "failed" && (
                      <span className="mt-1 block max-w-xs text-xs text-red-700 dark:text-red-400">
                        {dataset.lastJob.error}
                      </span>
                    )}
                  </td>
                  <td className={`${TD} text-right`}>
                    <Button
                      variant="ghost"
                      busy={isActive(dataset.lastJob)}
                      onClick={() => refresh.mutate({ id: dataset.id })}
                    >
                      Refresh
                    </Button>
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
