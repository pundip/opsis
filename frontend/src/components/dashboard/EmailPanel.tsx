// "Email me": the open dashboard emailed to the signed-in person on a schedule
// (FEATURE_EMAIL). Each email is built as they see the dashboard.
import { useState } from "react";
import { formatRun, useSetSubscription, useSubscription } from "../../lib/operations";
import { Button, Card, ErrorNote, Field, Select } from "../ui";

const OFF = "off";
const CUSTOM = "custom";

export function EmailPanel({ dashboardId }: { dashboardId: string }) {
  const current = useSubscription(dashboardId);
  const save = useSetSubscription();
  const [choice, setChoice] = useState<string | null>(null);
  const data = current.data;
  const existing = data?.subscription ?? null;
  const picked = choice ?? (existing ? (existing.preset ?? CUSTOM) : OFF);
  const cron =
    picked === OFF ? null : picked === CUSTOM ? (existing?.cron ?? null) : (data?.presets.find((p) => p.id === picked)?.cron ?? null);
  const changed = cron !== (existing?.cron ?? null);
  return (
    <Card className="mb-4 p-4">
      <h2 className="mb-1 font-medium">Email me this dashboard</h2>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        {data
          ? `Sent to ${data.email} with each visual's numbers as you see them (your access, the filters at their defaults) and a link back here. Times are in ${data.timezone}.`
          : "Loading..."}
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="When" htmlFor={`email-${dashboardId}`}>
          <Select id={`email-${dashboardId}`} className="!w-72" value={picked} onChange={(event) => setChoice(event.target.value)}>
            <option value={OFF}>Off</option>
            {(data?.presets ?? []).map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.label}
              </option>
            ))}
            {existing && !existing.preset && <option value={CUSTOM}>Custom ({existing.cron})</option>}
          </Select>
        </Field>
        <Button
          variant="primary"
          busy={save.isPending}
          disabled={!changed || !data}
          onClick={() => save.mutate({ dashboardId, cron }, { onSuccess: () => setChoice(null) })}
        >
          Save
        </Button>
      </div>
      {existing && data && (
        <p role="status" className="mt-2 text-xs text-zinc-500">
          Next email {formatRun(existing.nextRunAt, data.timezone)}.
        </p>
      )}
      <ErrorNote error={current.error ?? save.error} />
    </Card>
  );
}
