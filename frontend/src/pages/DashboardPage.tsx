// Viewing a dashboard. Editors see the draft by default and can switch to the published
// version. Filters, cross-filters and drill levels live in the URL, so a view can be
// bookmarked and shared. Admins can look at the data as someone else (View as).
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Canvas } from "../components/dashboard/Canvas";
import { EmailPanel } from "../components/dashboard/EmailPanel";
import { FiltersBar } from "../components/dashboard/FiltersBar";
import { SharePanel } from "../components/dashboard/SharePanel";
import { Badge, Button, ErrorNote, Input, Select } from "../components/ui";
import { useDirectory } from "../lib/access";
import type { AppConfig, Me } from "../lib/config";
import { type DataSource, type ViewAs, useDashboard, useDashboardVersion } from "../lib/dashboard";
import { useChartTheme } from "../lib/theme";
import { useUrlViewState } from "../lib/urlState";

function ViewAsControl({ config, value, onChange }: { config: AppConfig; value: ViewAs | null; onChange: (next: ViewAs | null) => void }) {
  const directory = useDirectory(config.authMode !== "none");
  const [choice, setChoice] = useState("");
  const [attributes, setAttributes] = useState('{"regions": []}');
  let parsed: Record<string, unknown> | null = null;
  try {
    const value_: unknown = JSON.parse(attributes);
    parsed = typeof value_ === "object" && value_ !== null && !Array.isArray(value_) ? (value_ as Record<string, unknown>) : null;
  } catch {
    parsed = null;
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        aria-label="View as"
        className="!w-56"
        value={choice}
        onChange={(event) => {
          const next = event.target.value;
          setChoice(next);
          if (next === "") onChange(null);
          else if (next.startsWith("user:")) {
            const user = directory.data?.users.find((u) => u.id === next.slice(5));
            if (user) onChange({ userId: user.id, label: `${user.name} (${user.role})` });
          }
        }}
      >
        <option value="">View as: yourself</option>
        {(directory.data?.users ?? []).map((user) => (
          <option key={user.id} value={`user:${user.id}`}>
            View as {user.name}
          </option>
        ))}
        <option value="custom">View as a viewer with attributes...</option>
      </Select>
      {choice === "custom" && (
        <>
          <Input aria-label="Attributes to view as" className="!w-64 font-mono" value={attributes} onChange={(e) => setAttributes(e.target.value)} />
          <Button
            disabled={parsed === null}
            onClick={() => parsed && onChange({ role: "viewer", attributes: parsed, label: `a viewer with ${JSON.stringify(parsed)}` })}
          >
            Apply
          </Button>
        </>
      )}
      {value && (
        <Button variant="ghost" onClick={() => { setChoice(""); onChange(null); }}>
          Stop
        </Button>
      )}
    </div>
  );
}

export function DashboardPage({ config, me }: { config: AppConfig; me: Me }) {
  const { id = "" } = useParams();
  const dashboard = useDashboard(id);
  const [state, setState] = useUrlViewState();
  const [showPublished, setShowPublished] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [emailing, setEmailing] = useState(false);
  const [viewAs, setViewAs] = useState<ViewAs | null>(null);
  const data = dashboard.data;
  const published = useDashboardVersion(id, showPublished && data ? data.publishedVersion : null);
  const spec = showPublished && published.data ? published.data.spec : data?.spec;
  const theme = useChartTheme(spec?.theme);
  const admin = me.permissions.includes("settings:manage");

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
  const source: DataSource = { kind: viewingPublished ? "published" : "draft", viewAs };
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
        {admin && <ViewAsControl config={config} value={viewAs} onChange={setViewAs} />}
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
          </>
        )}
        {config.features.email && config.authMode !== "none" && (
          <Button aria-expanded={emailing} onClick={() => setEmailing(!emailing)}>
            Email me
          </Button>
        )}
        {data.canShare && config.authMode !== "none" && (
          <Button aria-expanded={sharing} onClick={() => setSharing(!sharing)}>
            Share
          </Button>
        )}
        {data.canEdit && (
          <Link
            to={`/dashboards/${id}/edit`}
            className="rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-white dark:text-zinc-950"
          >
            Edit
          </Link>
        )}
      </div>
      {sharing && (
        <SharePanel dashboardId={id} config={config} me={me} published={data.publishedVersion !== null} canPublish={data.canPublish} />
      )}
      {emailing && <EmailPanel dashboardId={id} />}
      {viewAs && (
        <p role="status" className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm dark:border-amber-900 dark:bg-amber-950/40">
          Viewing as {viewAs.label}: row-level security applies as it would for them. This shows their data, not whether
          they may open the dashboard.
        </p>
      )}
      <FiltersBar dashboardId={id} spec={spec} source={source} state={state} onState={setState} />
      {spec.items.length === 0 ? (
        <p className="rounded-lg border border-dashed border-zinc-300 p-10 text-center text-sm text-zinc-500 dark:border-zinc-700">
          This dashboard is empty.{data.canEdit && " Choose Edit to add visuals."}
        </p>
      ) : (
        <Canvas dashboardId={id} spec={spec} source={source} state={state} onState={setState} theme={theme} calendar={config.calendar} />
      )}
    </div>
  );
}
