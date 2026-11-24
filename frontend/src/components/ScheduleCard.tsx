// A dataset's refresh schedule (FEATURE_SCHEDULED_REFRESH): a preset or a cron
// expression, in the workspace timezone, with the next runs shown before saving.
import { useState } from "react";
import { errorText } from "../lib/api";
import { type Schedule, formatRun, usePresets, useSchedulePreview, useSetSchedule } from "../lib/operations";
import { Button, Card, ErrorNote, Field, Input, Select } from "./ui";

const CUSTOM = "custom";
const OFF = "off";

export function ScheduleCard({ datasetId, schedule }: { datasetId: string; schedule: Schedule | null | undefined }) {
  const presets = usePresets();
  const save = useSetSchedule(datasetId);
  const initial = schedule ? (schedule.preset ?? CUSTOM) : OFF;
  const [choice, setChoice] = useState<string | null>(null);
  const [custom, setCustom] = useState<string | null>(null);
  const picked = choice ?? initial;
  const cron =
    picked === OFF ? "" : picked === CUSTOM ? (custom ?? schedule?.cron ?? "") : (presets.data?.presets.find((p) => p.id === picked)?.cron ?? "");
  const preview = useSchedulePreview(cron);
  const timezone = presets.data?.timezone ?? schedule?.timezone ?? "UTC";
  const changed = (picked === OFF ? null : cron) !== (schedule?.cron ?? null);

  return (
    <Card className="mb-5 p-4">
      <h2 className="mb-1 font-medium">Refresh schedule</h2>
      <p className="mb-3 text-xs text-zinc-500">
        {schedule
          ? `Next refresh ${formatRun(schedule.nextRunAt, timezone)} (${timezone}).`
          : `Not refreshed on a schedule. Times are in ${timezone}.`}
      </p>
      {schedule?.paused && (
        <p role="alert" className="mb-3 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          {schedule.paused}
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="When" htmlFor={`schedule-${datasetId}`}>
          <Select
            id={`schedule-${datasetId}`}
            value={picked}
            onChange={(event) => {
              setChoice(event.target.value);
              if (event.target.value === CUSTOM && custom === null) setCustom(schedule?.cron ?? "0 6 * * *");
            }}
          >
            <option value={OFF}>Off</option>
            {(presets.data?.presets ?? []).map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.label}
              </option>
            ))}
            <option value={CUSTOM}>Custom (cron)</option>
          </Select>
        </Field>
        {picked === CUSTOM && (
          <Field label="Cron expression" htmlFor={`cron-${datasetId}`} hint="Minute, hour, day, month, weekday. At most every 15 minutes.">
            <Input
              id={`cron-${datasetId}`}
              className="font-mono"
              value={custom ?? ""}
              onChange={(event) => setCustom(event.target.value)}
            />
          </Field>
        )}
      </div>
      {cron && (
        <p role="status" className="mt-2 text-xs text-zinc-600 dark:text-zinc-400">
          {preview.isError
            ? errorText(preview.error)
            : preview.data
              ? `Next runs: ${preview.data.next.map((t) => formatRun(t, timezone)).join("; ")}`
              : "Working out the next runs..."}
        </p>
      )}
      <ErrorNote error={presets.error ?? save.error} />
      <Button
        className="mt-3"
        variant="primary"
        busy={save.isPending}
        disabled={!changed || (picked !== OFF && !preview.data)}
        onClick={() =>
          save.mutate(picked === OFF ? null : cron, {
            onSuccess: () => {
              setChoice(null);
              setCustom(null);
            },
          })
        }
      >
        Save schedule
      </Button>
    </Card>
  );
}
