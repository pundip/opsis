// Admin settings: backups, the email check, the demo data, and which features this
// install has on.
// Everything here is set in .env; the page shows it and runs the on-demand parts.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { Button, Card, Empty, ErrorNote, PageHeader, ROW, TD, TH } from "../components/ui";
import { apiPost } from "../lib/api";
import type { AppConfig, Features } from "../lib/config";
import { formatBytes, formatDateTime } from "../lib/format";
import { formatRun, useBackUpNow, useBackups, useDemo, useLoadDemo } from "../lib/operations";

const FLAGS: { key: keyof Features; env: string; label: string }[] = [
  { key: "scheduledRefresh", env: "FEATURE_SCHEDULED_REFRESH", label: "Refresh schedules" },
  { key: "backups", env: "FEATURE_BACKUPS", label: "Backups" },
  { key: "email", env: "FEATURE_EMAIL", label: "Email" },
  { key: "publicShareLinks", env: "FEATURE_PUBLIC_SHARE_LINKS", label: "Public share links" },
  { key: "assistant", env: "FEATURE_ASSISTANT", label: "Assistant in the editor" },
  { key: "sqlLab", env: "FEATURE_SQL_LAB", label: "SQL Lab" },
];

function BackupsCard({ config }: { config: AppConfig }) {
  const [waiting, setWaiting] = useState(false);
  const list = useBackups(true);
  const now = useBackUpNow();
  // After "Back up now", look again every few seconds until the new file is there.
  const count = list.data?.backups.length ?? 0;
  const [before, setBefore] = useState<number | null>(null);
  useEffect(() => {
    if (!waiting) return;
    if (before !== null && count > before) {
      setWaiting(false);
      return;
    }
    const timer = window.setTimeout(() => void list.refetch(), 3000);
    return () => window.clearTimeout(timer);
  }, [waiting, before, count, list]);

  const data = list.data;
  return (
    <Card className="mb-5 p-4">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="mr-auto font-medium">Backups</h2>
        <Button
          variant="primary"
          busy={now.isPending || waiting}
          onClick={() =>
            now.mutate(undefined, {
              onSuccess: () => {
                setBefore(count);
                setWaiting(true);
              },
            })
          }
        >
          {waiting ? "Backing up" : "Back up now"}
        </Button>
      </div>
      {data && (
        <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
          {data.schedule ? `Next backup ${formatRun(data.schedule.nextRunAt, data.schedule.timezone)} (${data.schedule.cron}). ` : ""}
          Each backup holds the metadata database, the uploaded files and every dataset&apos;s current data. The newest{" "}
          {data.retention} are kept in <code className="font-mono">{data.folder}</code>. Keep your <code>.env</code> safe as well:
          the encryption key in it is not in the backups.
        </p>
      )}
      <ErrorNote error={list.error ?? now.error} />
      {data && data.backups.length === 0 ? (
        <Empty>No backups yet.</Empty>
      ) : (
        <table className="w-full">
          <thead>
            <tr>
              <th className={TH}>File</th>
              <th className={TH}>Size</th>
              <th className={TH}>Made</th>
            </tr>
          </thead>
          <tbody>
            {(data?.backups ?? []).map((backup) => (
              <tr key={backup.name} className={ROW}>
                <td className={`${TD} font-mono text-xs`}>{backup.name}</td>
                <td className={TD}>{formatBytes(backup.bytes)}</td>
                <td className={TD}>{formatDateTime(backup.createdAt, config.calendar.timezone)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <details className="mt-3 text-sm">
        <summary className="cursor-pointer text-brand">How to restore</summary>
        <p className="mt-2 text-zinc-600 dark:text-zinc-400">
          On the server, stop the app, restore, and start it again. The previous data is moved aside, not deleted:
        </p>
        <pre className="mt-1 overflow-auto rounded bg-zinc-50 p-2 font-mono text-xs dark:bg-zinc-950">
          {`docker compose stop api worker\ndocker compose run --rm api restore <file>.tar --yes\ndocker compose up -d`}
        </pre>
      </details>
    </Card>
  );
}

function EmailCard() {
  const test = useMutation({ mutationFn: () => apiPost<{ sentTo: string }>("/api/admin/email/test") });
  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">Email</h2>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Invitations, password resets and emailed dashboards go out through the mail server in <code>.env</code> (SMTP_HOST).
      </p>
      <ErrorNote error={test.error} />
      {test.data && <p role="status" className="mb-2 text-sm">A test email was sent to {test.data.sentTo}.</p>}
      <Button busy={test.isPending} onClick={() => test.mutate()}>
        Send me a test email
      </Button>
    </Card>
  );
}

function DemoCard() {
  const demo = useDemo();
  const load = useLoadDemo();
  const [waiting, setWaiting] = useState(false);
  // The job takes a few seconds: look again until the dashboard is there.
  useEffect(() => {
    if (!waiting) return;
    if (demo.data?.loaded) {
      setWaiting(false);
      return;
    }
    const timer = window.setTimeout(() => void demo.refetch(), 2000);
    return () => window.clearTimeout(timer);
  }, [waiting, demo]);
  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">Demo data</h2>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Three made-up datasets (gifts, donors and regions, joined to each other) with their models, and a published
        dashboard, to try things out without connecting a source. Remove them by deleting the dashboard and the three demo datasets.
      </p>
      <ErrorNote error={demo.error ?? load.error} />
      {demo.data?.loaded && demo.data.dashboardId ? (
        <Link to={`/dashboards/${demo.data.dashboardId}`} className="text-sm text-brand underline">
          Open the demo dashboard
        </Link>
      ) : (
        <Button
          busy={load.isPending || waiting}
          disabled={!demo.data}
          onClick={() => load.mutate(undefined, { onSuccess: () => setWaiting(true) })}
        >
          {waiting ? "Loading the demo data" : "Load demo data"}
        </Button>
      )}
    </Card>
  );
}

export function Settings({ config }: { config: AppConfig }) {
  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader title="Settings" description="Set in .env on the server; restart the stack after a change." />
      {config.features.backups && <BackupsCard config={config} />}
      {config.features.email && <EmailCard />}
      <DemoCard />
      <Card className="p-4">
        <h2 className="mb-2 font-medium">This install</h2>
        <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          <div className="flex justify-between gap-2">
            <dt className="text-zinc-500">DEPLOY_MODE</dt>
            <dd>{config.deployMode}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-zinc-500">AUTH_MODE</dt>
            <dd>{config.authMode}</dd>
          </div>
          {FLAGS.map((flag) => (
            <div key={flag.key} className="flex justify-between gap-2">
              <dt className="text-zinc-500" title={flag.env}>
                {flag.label}
              </dt>
              <dd>{config.features[flag.key] ? "on" : "off"}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </div>
  );
}
