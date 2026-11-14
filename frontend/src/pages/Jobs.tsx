import { Link } from "react-router-dom";
import type { AppConfig } from "../lib/config";
import { useDatasets, useJobs } from "../lib/data";
import { Card, Empty, ErrorNote, PageHeader, TH } from "../components/ui";
import { JobRow } from "./DatasetDetail";

export function Jobs({ config }: { config: AppConfig }) {
  const jobs = useJobs();
  const datasets = useDatasets();
  const byId = new Map(datasets.data?.map((d) => [d.id, d]));

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Jobs"
        description={`Background work, newest first. Jobs run ${
          config.jobRunner === "inline" ? "inside the app" : "in the worker container"
        }, one at a time.`}
      />
      <ErrorNote error={jobs.error} />
      <Card>
        {jobs.data?.length ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className={TH}>Dataset</th>
                  <th className={TH}>Status</th>
                  <th className={TH}>Requested</th>
                  <th className={TH}>Took</th>
                  <th className={`${TH} text-right`}>Rows</th>
                  <th className={TH}>Error</th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.map((job) => {
                  const dataset = job.datasetId ? byId.get(job.datasetId) : undefined;
                  return (
                    <JobRow
                      key={job.id}
                      job={job}
                      timezone={config.calendar.timezone}
                      name={
                        dataset ? (
                          <Link to={`/datasets/${dataset.id}`} className="text-brand underline">
                            {dataset.label}
                          </Link>
                        ) : (
                          <span className="text-zinc-500">Deleted dataset</span>
                        )
                      }
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No jobs have run yet.</Empty>
        )}
      </Card>
    </div>
  );
}
