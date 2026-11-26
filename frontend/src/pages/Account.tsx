// Your account: who you are, your password, and signing out everywhere.
// The same password form is shown on its own when a new password is required.
import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiKeys } from "../components/ApiKeys";
import { Badge, Button, Card, Empty, ErrorNote, Field, Input, PageHeader, ROW, TD, TH } from "../components/ui";
import { useChangePassword, useSignOut } from "../lib/access";
import type { AppConfig, Me } from "../lib/config";
import { formatRun, scheduleLabel, useMySubscriptions, useSetSubscription } from "../lib/operations";
import { AuthFrame } from "./Login";
import { MIN_PASSWORD } from "./SetPassword";

function PasswordForm({ onDone }: { onDone?: () => void }) {
  const change = useChangePassword();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [done, setDone] = useState(false);
  const mismatch = repeat.length > 0 && next !== repeat;
  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (next !== repeat) return;
        change.mutate(
          { currentPassword: current, newPassword: next },
          {
            onSuccess: () => {
              setCurrent("");
              setNext("");
              setRepeat("");
              setDone(true);
              onDone?.();
            },
          },
        );
      }}
    >
      <Field label="Current password" htmlFor="pw-current">
        <Input id="pw-current" type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
      </Field>
      <Field label="New password" htmlFor="pw-new" hint={`At least ${MIN_PASSWORD} characters.`}>
        <Input id="pw-new" type="password" autoComplete="new-password" required minLength={MIN_PASSWORD} value={next} onChange={(e) => setNext(e.target.value)} />
      </Field>
      <Field label="Repeat the new password" htmlFor="pw-repeat" error={mismatch ? "The two passwords are different." : undefined}>
        <Input id="pw-repeat" type="password" autoComplete="new-password" required value={repeat} onChange={(e) => setRepeat(e.target.value)} />
      </Field>
      <ErrorNote error={change.error} />
      {done && <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">Password changed. Your other sessions have been signed out.</p>}
      <Button type="submit" variant="primary" busy={change.isPending} disabled={mismatch}>
        Change password
      </Button>
    </form>
  );
}

function EmailedDashboards() {
  const list = useMySubscriptions();
  const stop = useSetSubscription();
  const rows = list.data?.subscriptions ?? [];
  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">Emailed dashboards</h2>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        Dashboards emailed to you on a schedule. Start one with Email me on a dashboard.
      </p>
      <ErrorNote error={list.error ?? stop.error} />
      {list.isSuccess && rows.length === 0 ? (
        <Empty>None yet.</Empty>
      ) : (
        <table className="w-full">
          <thead>
            <tr>
              <th className={TH}>Dashboard</th>
              <th className={TH}>When</th>
              <th className={TH}>Next email</th>
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.dashboardId} className={ROW}>
                <td className={TD}>
                  <Link to={`/dashboards/${row.dashboardId}`} className="text-brand underline">
                    {row.title}
                  </Link>
                </td>
                <td className={TD}>{scheduleLabel(row, list.data?.presets ?? [])}</td>
                <td className={TD}>{formatRun(row.nextRunAt, row.timezone)}</td>
                <td className={`${TD} text-right`}>
                  <Button
                    variant="ghost"
                    aria-label={`Stop emailing ${row.title}`}
                    busy={stop.isPending && stop.variables?.dashboardId === row.dashboardId}
                    onClick={() => stop.mutate({ dashboardId: row.dashboardId, cron: null })}
                  >
                    Stop
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

/** Shown instead of the app until the first password is replaced. */
export function ForcedPasswordChange({ config }: { config: AppConfig }) {
  const signOut = useSignOut();
  return (
    <AuthFrame config={config} title="Choose a new password">
      <p className="mb-4 text-sm text-zinc-600 dark:text-zinc-400">
        You signed in with a password that was set for you. Choose your own before continuing.
      </p>
      <PasswordForm />
      <button type="button" className="mt-4 text-sm text-brand underline" onClick={() => signOut.mutate()}>
        Sign out
      </button>
    </AuthFrame>
  );
}

export function Account({ config, me }: { config: AppConfig; me: Me }) {
  const signOutEverywhere = useSignOut(true);
  const signIn = config.authMode !== "none";
  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader title="Account" />
      <Card className="mb-5 p-4">
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2 text-sm">
          <dt className="text-zinc-500">Name</dt>
          <dd>{me.displayName}</dd>
          <dt className="text-zinc-500">Email</dt>
          <dd>{me.email}</dd>
          <dt className="text-zinc-500">Role</dt>
          <dd>
            <Badge>{me.role}</Badge>
          </dd>
          {me.google && (
            <>
              <dt className="text-zinc-500">Google</dt>
              <dd>Linked: you can sign in with Google.</dd>
            </>
          )}
        </dl>
        <p className="mt-4 text-sm">
          <Link to="/tokens" className="text-brand underline">
            Access tokens
          </Link>{" "}
          let AI clients and scripts work in {config.appName} as you.
        </p>
      </Card>
      {config.features.assistant && me.permissions.includes("dashboards:edit") && <ApiKeys />}
      {signIn && (
        <>
          <Card className="mb-5 p-4">
            <h2 className="mb-3 font-medium">Password</h2>
            {me.hasPassword ? (
              <PasswordForm />
            ) : (
              <p className="text-sm text-zinc-600 dark:text-zinc-400">
                You sign in with Google, so there is no password to change here.
              </p>
            )}
          </Card>
          {config.features.email && <EmailedDashboards />}
          <Card className="p-4">
            <h2 className="mb-1 font-medium">Sessions</h2>
            <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
              Sign out on every device and browser, including this one. Access tokens keep working; revoke them on their own page.
            </p>
            <ErrorNote error={signOutEverywhere.error} />
            <Button variant="danger" busy={signOutEverywhere.isPending} onClick={() => signOutEverywhere.mutate()}>
              Sign out everywhere
            </Button>
          </Card>
        </>
      )}
    </div>
  );
}
