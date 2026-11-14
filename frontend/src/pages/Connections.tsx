import { useState } from "react";
import { ApiError } from "../lib/api";
import type { AppConfig, Me } from "../lib/config";
import {
  type Connection,
  type Connector,
  type TestResult,
  useConnections,
  useConnectors,
  useDeleteConnection,
  useSaveConnection,
  useTestConnection,
  useTestSettings,
} from "../lib/data";
import { timeAgo } from "../lib/format";
import { SchemaForm, schemaDefaults } from "../components/SchemaForm";
import {
  Badge, Button, Card, Empty, ErrorNote, Field, Input, PageHeader, ROW, Select, TD, TH,
} from "../components/ui";

function TestNote({ result }: { result: TestResult | undefined }) {
  if (!result) return null;
  return (
    <p
      role="status"
      className={`text-sm ${result.ok ? "text-emerald-700 dark:text-emerald-400" : "text-red-700 dark:text-red-400"}`}
    >
      {result.ok ? "Connection works. " : "Connection failed. "}
      {result.message}
    </p>
  );
}

interface FormProps {
  connectors: Connector[];
  editing: Connection | null;
  maxUploadMb: number;
  onDone: () => void;
}

function ConnectionForm({ connectors, editing, maxUploadMb, onDone }: FormProps) {
  const [connectorId, setConnectorId] = useState(editing?.connectorId ?? connectors[0]?.id ?? "");
  const connector = connectors.find((c) => c.id === connectorId);
  const [name, setName] = useState(editing?.name ?? "");
  const [values, setValues] = useState<Record<string, unknown>>(
    editing ? editing.config : connector ? schemaDefaults(connector.configSchema) : {},
  );
  const save = useSaveConnection(editing?.id ?? null);
  const test = useTestSettings();
  const details = save.error instanceof ApiError ? save.error.details : [];

  if (!connector) return <ErrorNote error={new Error("This connector is not installed.")} />;
  const input = { name, connectorId, config: values };

  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-4 text-base font-semibold">
        {editing ? `Edit ${editing.name}` : "New connection"}
      </h2>
      <form
        className="grid max-w-xl gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate(input, { onSuccess: onDone });
        }}
      >
        <Field label="Type" htmlFor="conn-type">
          <Select
            id="conn-type"
            value={connectorId}
            disabled={editing !== null}
            onChange={(event) => {
              const next = connectors.find((c) => c.id === event.target.value);
              setConnectorId(event.target.value);
              setValues(next ? schemaDefaults(next.configSchema) : {});
              test.reset();
              save.reset();
            }}
          >
            {connectors.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Name" htmlFor="conn-name" required hint="How this source appears in Opsis.">
          <Input id="conn-name" value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <SchemaForm
          schema={connector.configSchema}
          values={values}
          onChange={setValues}
          savedSecrets={editing?.secretFields}
          details={details}
          idPrefix="conn"
          maxUploadMb={maxUploadMb}
        />
        <TestNote result={test.data} />
        <ErrorNote error={test.error ?? (details.length ? null : save.error)} />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" busy={save.isPending}>
            {editing ? "Save changes" : "Save connection"}
          </Button>
          <Button
            busy={test.isPending}
            onClick={() => test.mutate({ ...input, connectionId: editing?.id ?? null })}
          >
            Test
          </Button>
          <Button variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

export function Connections({ config, me }: { config: AppConfig; me: Me }) {
  const connections = useConnections();
  const connectors = useConnectors();
  const test = useTestConnection();
  const remove = useDeleteConnection();
  const [form, setForm] = useState<{ editing: Connection | null } | null>(null);
  const canManage = me.permissions.includes("connections:manage");

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Connections"
        description="Sources that Opsis pulls data from. Passwords are stored encrypted and are never shown again."
        actions={
          canManage &&
          !form && (
            <Button variant="primary" onClick={() => setForm({ editing: null })}>
              New connection
            </Button>
          )
        }
      />
      {form && connectors.data && (
        <ConnectionForm
          key={form.editing?.id ?? "new"}
          connectors={connectors.data}
          editing={form.editing}
          maxUploadMb={config.maxUploadMb}
          onDone={() => setForm(null)}
        />
      )}
      <ErrorNote error={connections.error ?? connectors.error ?? remove.error} />
      <Card>
        {connections.data?.length === 0 ? (
          <Empty>No connections yet. Add one to start bringing data in.</Empty>
        ) : (
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Name</th>
                <th className={TH}>Type</th>
                <th className={TH}>Last test</th>
                <th className={TH}>Datasets</th>
                <th className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {connections.data?.map((connection) => (
                <tr key={connection.id} className={ROW}>
                  <td className={`${TD} font-medium`}>{connection.name}</td>
                  <td className={TD}>{connection.connectorName}</td>
                  <td className={TD}>
                    {!connection.secretsReadable ? (
                      <Badge tone="bad">Secrets unreadable</Badge>
                    ) : connection.lastTest ? (
                      <span title={connection.lastTest.message}>
                        <Badge tone={connection.lastTest.ok ? "good" : "bad"}>
                          {connection.lastTest.ok ? "OK" : "Failed"}
                        </Badge>{" "}
                        <span className="text-xs text-zinc-500">
                          {timeAgo(connection.lastTest.at)}
                        </span>
                        {!connection.lastTest.ok && (
                          <span className="mt-1 block max-w-xs text-xs text-red-700 dark:text-red-400">
                            {connection.lastTest.message}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="text-xs text-zinc-500">Not tested</span>
                    )}
                  </td>
                  <td className={TD}>{connection.datasetCount}</td>
                  <td className={`${TD} text-right whitespace-nowrap`}>
                    {canManage && (
                      <span className="inline-flex gap-1">
                        <Button
                          variant="ghost"
                          busy={test.isPending && test.variables === connection.id}
                          onClick={() => test.mutate(connection.id)}
                        >
                          Test
                        </Button>
                        <Button variant="ghost" onClick={() => setForm({ editing: connection })}>
                          Edit
                        </Button>
                        <Button
                          variant="ghost"
                          disabled={connection.datasetCount > 0}
                          title={
                            connection.datasetCount > 0 ? "Delete its datasets first" : undefined
                          }
                          onClick={() => {
                            if (window.confirm(`Delete the connection "${connection.name}"?`)) {
                              remove.mutate(connection.id);
                            }
                          }}
                        >
                          Delete
                        </Button>
                      </span>
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
