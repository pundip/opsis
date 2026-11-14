// Viewing a dashboard. Editors see the draft by default and can switch to the published version.
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Canvas } from "../components/dashboard/Canvas";
import { FiltersBar } from "../components/dashboard/FiltersBar";
import { Badge, ErrorNote } from "../components/ui";
import type { AppConfig } from "../lib/config";
import { EMPTY_STATE, type ViewState, useDashboard, useDashboardVersion } from "../lib/dashboard";
import { useChartTheme } from "../lib/theme";

export function DashboardPage({ config }: { config: AppConfig }) {
  const { id = "" } = useParams();
  const dashboard = useDashboard(id);
  const [state, setState] = useState<ViewState>(EMPTY_STATE);
  const [showPublished, setShowPublished] = useState(false);
  const data = dashboard.data;
  const published = useDashboardVersion(id, showPublished && data ? data.publishedVersion : null);
  const spec = showPublished && published.data ? published.data.spec : data?.spec;
  const theme = useChartTheme(spec?.theme);

  if (dashboard.isPending) return <p className="text-sm text-zinc-500">Loading...</p>;
  if (dashboard.isError || !data || !spec) {
    return (
      <div className="mx-auto max-w-5xl">
        <ErrorNote error={dashboard.error} />
        <Link to="/dashboards" className="mt-3 inline-block text-sm text-brand underline">
          Back to dashboards
        </Link>
      </div>
    );
  }
  const viewingPublished = data.source === "published" || showPublished;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="mr-auto">
          <p className="text-sm">
            <Link to="/dashboards" className="text-brand underline">
              Dashboards
            </Link>
          </p>
          <h1 className="text-2xl font-semibold">{spec.title}</h1>
          {spec.description && <p className="text-sm text-zinc-600 dark:text-zinc-400">{spec.description}</p>}
        </div>
        {data.canEdit && (
          <>
            {viewingPublished || (data.publishedVersion !== null && !data.hasUnpublishedChanges) ? (
              <Badge tone="good">Published version</Badge>
            ) : (
              <Badge>Draft</Badge>
            )}
            {data.publishedVersion !== null && data.hasUnpublishedChanges && (
              <button type="button" className="text-sm text-brand underline" onClick={() => setShowPublished(!showPublished)}>
                {showPublished ? "Show the draft" : "Show the published version"}
              </button>
            )}
            <Link
              to={`/dashboards/${id}/edit`}
              className="rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-white dark:text-zinc-950"
            >
              Edit
            </Link>
          </>
        )}
      </div>
      <FiltersBar dashboardId={id} spec={spec} source={{ kind: viewingPublished ? "published" : "draft" }} state={state} onState={setState} />
      {spec.items.length === 0 ? (
        <p className="rounded-lg border border-dashed border-zinc-300 p-10 text-center text-sm text-zinc-500 dark:border-zinc-700">
          This dashboard is empty.{data.canEdit && " Choose Edit to add visuals."}
        </p>
      ) : (
        <Canvas
          dashboardId={id}
          spec={spec}
          source={{ kind: viewingPublished ? "published" : "draft" }}
          state={state}
          onState={setState}
          theme={theme}
          calendar={config.calendar}
        />
      )}
    </div>
  );
}
