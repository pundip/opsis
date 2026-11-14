import type { AppConfig, Features, Me } from "../lib/config";
import { fiscalYearLabel, monthName, todayIn } from "../lib/calendar";

const FEATURE_LABELS: Record<keyof Features, string> = {
  assistant: "In-app assistant",
  sqlLab: "SQL Lab",
  scheduledRefresh: "Scheduled refresh",
  publicShareLinks: "Public share links",
  backups: "Nightly backups",
  email: "Email",
};

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1 text-sm">
      <dt className="text-zinc-600 dark:text-zinc-400">{label}</dt>
      <dd className="text-right font-medium">{value}</dd>
    </div>
  );
}

export function Home({ config, me }: { config: AppConfig; me: Me }) {
  const today = todayIn(config.calendar.timezone);
  const currentFy = fiscalYearLabel(today.year, today.month, config.calendar);
  const features = Object.keys(FEATURE_LABELS) as (keyof Features)[];
  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">{config.appName}</h1>
      <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
        {config.branding.tagline ?? "Self-hosted business intelligence"}. Signed in as{" "}
        {me.displayName} ({me.role}).
      </p>

      <div className="mt-6 grid gap-4 md:grid-cols-3">
        <Card title="Deployment">
          <dl>
            <Row label="Mode" value={config.deployMode} />
            <Row label="Sign-in" value={config.authMode} />
            <Row label="Job runner" value={config.jobRunner} />
            <Row label="MCP auth" value={config.mcpAuth} />
            <Row label="Assistant" value={config.assistantProvider ?? "off"} />
          </dl>
        </Card>

        <Card title="Features">
          <ul>
            {features.map((key) => (
              <li
                key={key}
                data-feature={key}
                data-enabled={config.features[key]}
                className="flex items-center justify-between py-1 text-sm"
              >
                <span className="text-zinc-600 dark:text-zinc-400">{FEATURE_LABELS[key]}</span>
                <span
                  className={
                    config.features[key]
                      ? "rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                      : "rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
                  }
                >
                  {config.features[key] ? "On" : "Off"}
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Calendar">
          <dl>
            <Row label="Timezone" value={config.calendar.timezone} />
            <Row
              label="Week starts"
              value={config.calendar.weekStart === "monday" ? "Monday" : "Sunday"}
            />
            <Row
              label="Financial year starts"
              value={monthName(config.calendar.fiscalYearStartMonth)}
            />
            <Row label="Current financial year" value={currentFy} />
          </dl>
        </Card>
      </div>

      <p className="mt-6 text-sm text-zinc-600 dark:text-zinc-400">
        This is the application shell. Connections, datasets and dashboards arrive in the next
        milestones. Features are switched in <code>.env</code>; restart the stack to apply a change.
      </p>
    </div>
  );
}
