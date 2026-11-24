// Schedules (refresh schedules, emailed dashboards, the backup and the clean-up) and backups.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "./api";
import type { Job } from "./data";

export interface Schedule {
  cron: string;
  /** The preset it matches, if any. */
  preset: string | null;
  nextRunAt: string;
  lastRunAt: string | null;
  timezone: string;
  /** Why scheduled runs are paused, if they are (a connection needs reconnecting). */
  paused?: string | null;
}

export interface Preset {
  id: string;
  label: string;
  cron: string;
}

export interface ScheduleRow extends Schedule {
  id: string;
  kind: "refresh" | "snapshot" | "backup" | "cleanup";
  label: string;
  datasetId?: string;
  dataset?: string;
  dashboardId?: string;
}

export const usePresets = () =>
  useQuery({
    queryKey: ["schedule-presets"],
    queryFn: () => apiGet<{ presets: Preset[]; timezone: string }>("/api/schedules/presets"),
    staleTime: Infinity,
  });

export const useSchedules = () =>
  useQuery({
    queryKey: ["schedules"],
    queryFn: () => apiGet<{ schedules: ScheduleRow[]; timezone: string }>("/api/schedules"),
  });

/** The next run times of a cron expression, or the problem with it. */
export function useSchedulePreview(cron: string) {
  return useQuery({
    queryKey: ["schedule-preview", cron],
    queryFn: () => apiPost<{ cron: string; next: string[] }>("/api/schedules/preview", { cron }),
    enabled: cron.trim().length > 0,
    retry: false,
  });
}

export function useSetSchedule(datasetId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (cron: string | null) =>
      apiPut<{ schedule: Schedule | null }>(`/api/datasets/${datasetId}/schedule`, { cron }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["datasets", datasetId] });
      void client.invalidateQueries({ queryKey: ["schedules"] });
    },
  });
}

export interface Backups {
  backups: { name: string; bytes: number; createdAt: string }[];
  schedule: Schedule | null;
  retention: number;
  folder: string;
}

export const useBackups = (enabled: boolean) =>
  useQuery({
    queryKey: ["backups"],
    queryFn: () => apiGet<Backups>("/api/admin/backups"),
    enabled,
  });

export function useBackUpNow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<Job>("/api/admin/backups"),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["jobs"] }),
  });
}

/** "Tue 25 Nov, 06:00" in the workspace timezone (the times carry its offset). */
export function formatRun(iso: string, timezone: string): string {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(iso));
}

// ---- emailed dashboards (FEATURE_EMAIL with sign-in) ----------------------------------

export interface Subscription {
  subscription: Schedule | null;
  /** Where the email goes: the signed-in person's address. */
  email: string;
  presets: Preset[];
  timezone: string;
}

export interface MySubscription extends Schedule {
  dashboardId: string;
  title: string;
}

export const useSubscription = (dashboardId: string) =>
  useQuery({
    queryKey: ["subscription", dashboardId],
    queryFn: () => apiGet<Subscription>(`/api/dashboards/${dashboardId}/subscription`),
  });

export function useSetSubscription() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ dashboardId, cron }: { dashboardId: string; cron: string | null }) =>
      apiPut<Subscription>(`/api/dashboards/${dashboardId}/subscription`, { cron }),
    onSuccess: (data, { dashboardId }) => {
      client.setQueryData(["subscription", dashboardId], data);
      void client.invalidateQueries({ queryKey: ["my-subscriptions"] });
      void client.invalidateQueries({ queryKey: ["schedules"] });
    },
  });
}

export const useMySubscriptions = () =>
  useQuery({
    queryKey: ["my-subscriptions"],
    queryFn: () => apiGet<{ subscriptions: MySubscription[]; presets: Preset[]; timezone: string }>("/api/me/subscriptions"),
  });

/** "Mondays at 07:00", or the cron expression when no preset matches. */
export function scheduleLabel(schedule: Schedule, presets: Preset[]): string {
  return presets.find((p) => p.id === schedule.preset)?.label ?? `cron ${schedule.cron}`;
}

// ---- demo data -----------------------------------------------------------------------

export const useDemo = () =>
  useQuery({
    queryKey: ["demo"],
    queryFn: () => apiGet<{ loaded: boolean; dashboardId: string | null }>("/api/admin/demo"),
  });

export function useLoadDemo() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<Job>("/api/admin/demo"),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["jobs"] }),
  });
}
