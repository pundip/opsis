// Every schedule the person can see: dataset refreshes, their emailed dashboards,
// and for admins everyone's, the backup and the clean-up. Each refresh is changed
// on its dataset's page, each emailed dashboard on the dashboard (Email me).
import { Link } from "react-router-dom";
import { Card, Empty, ErrorNote, PageHeader, ROW, TD, TH } from "../components/ui";
import { formatRun, useSchedules } from "../lib/operations";
import { timeAgo } from "../lib/format";

export function Schedules() {
  const schedules = useSchedules();
  const rows = schedules.data?.schedules ?? [];
  const timezone = schedules.data?.timezone ?? "UTC";
  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Schedules"
        description={`What runs on its own, and when. Times are in ${timezone}. Set a dataset's refresh schedule on its page.`}
      />
      <ErrorNote error={schedules.error} />
      {schedules.isSuccess && rows.length === 0 ? (
        <Empty>No dataset is refreshed on a schedule yet. Open a dataset to set one.</Empty>
      ) : (
        <Card>
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>What</th>
                <th className={TH}>When</th>
                <th className={TH}>Next run</th>
                <th className={TH}>Last run</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={ROW}>
                  <td className={TD}>
                    {row.datasetId || row.dashboardId ? (
                      <Link
                        to={row.datasetId ? `/datasets/${row.datasetId}` : `/dashboards/${row.dashboardId ?? ""}`}
                        className="text-brand underline"
                      >
                        {row.label}
                      </Link>
                    ) : (
                      row.label
                    )}
                    {row.kind === "refresh" && <span className="ml-1 text-xs text-zinc-500">refresh</span>}
                    {row.paused && <span className="mt-1 block text-xs text-amber-800 dark:text-amber-300">{row.paused}</span>}
                  </td>
                  <td className={`${TD} font-mono text-xs`}>{row.cron}</td>
                  <td className={TD}>{formatRun(row.nextRunAt, timezone)}</td>
                  <td className={TD}>{row.lastRunAt ? timeAgo(row.lastRunAt) : "Not yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
