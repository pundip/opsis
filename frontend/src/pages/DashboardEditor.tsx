// The dashboard editor: a grid to arrange visuals, side panels to configure them,
// undo and redo, version history, and a JSON tab onto the same spec.
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Canvas } from "../components/dashboard/Canvas";
import { FiltersPanel, HistoryPanel, ItemPanel, SettingsPanel } from "../components/dashboard/EditorPanels";
import { FiltersBar } from "../components/dashboard/FiltersBar";
import { Badge, Button, ErrorNote, Input } from "../components/ui";
import { ApiError } from "../lib/api";
import type { AppConfig } from "../lib/config";
import {
  type DashboardDetail,
  type Item,
  type Manifest,
  type Spec,
  type ViewState,
  EMPTY_STATE,
  defaultConfig,
  newId,
  useDashboard,
  useFieldsFor,
  usePublishDashboard,
  useSaveDashboard,
  useSchemas,
  useValidateSpec,
} from "../lib/dashboard";
import { useHistory } from "../lib/history";
import { useModels } from "../lib/semantic";
import { useChartTheme } from "../lib/theme";
import { visualList } from "../visuals/registry";

const MonacoJson = lazy(() => import("../components/dashboard/MonacoJson"));

type Tab = "visual" | "filters" | "dashboard" | "history" | "json";
interface Problem {
  path: string;
  message: string;
}

function ProblemList({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) return null;
  return (
    <ul role="alert" className="mb-3 space-y-1 rounded-md border border-red-200 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950">
      {problems.map((problem) => (
        <li key={problem.path + problem.message} className="text-red-800 dark:text-red-300">
          <code className="font-mono text-xs">{problem.path}</code> {problem.message}
        </li>
      ))}
    </ul>
  );
}

function JsonTab({ spec, onApply, dark }: { spec: Spec; onApply: (spec: Spec) => void; dark: boolean }) {
  const schemas = useSchemas();
  const validate = useValidateSpec();
  const pretty = useMemo(() => JSON.stringify(spec, null, 2), [spec]);
  const [text, setText] = useState(pretty);
  const [problems, setProblems] = useState<Problem[]>([]);
  useEffect(() => {
    setText(pretty);
    setProblems([]);
  }, [pretty]);

  const apply = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      // The message names the position, for example "... at position 120 (line 9 column 3)".
      setProblems([{ path: "/", message: `This is not valid JSON: ${(error as Error).message}` }]);
      return;
    }
    validate.mutate(parsed, {
      onSuccess: (result) => {
        setProblems(result.problems);
        if (result.valid) onApply(parsed as Spec);
      },
    });
  };

  return (
    <div className="flex h-full flex-col gap-2">
      <p className="text-xs text-zinc-500">
        The whole dashboard as JSON. Apply checks it first; nothing changes if it is not valid.
      </p>
      <div className="min-h-80 flex-1">
        {schemas.data && (
          <Suspense fallback={<p className="text-sm text-zinc-500">Loading the editor...</p>}>
            <MonacoJson value={text} onChange={setText} schemas={schemas.data} main="dashboard.schema.json" dark={dark} label="Dashboard definition as JSON" />
          </Suspense>
        )}
      </div>
      <ProblemList problems={problems} />
      <ErrorNote error={validate.error ?? schemas.error} />
      <div className="flex gap-2">
        <Button variant="primary" busy={validate.isPending} disabled={text === pretty} onClick={apply}>
          Apply
        </Button>
        <Button variant="ghost" disabled={text === pretty} onClick={() => { setText(pretty); setProblems([]); }}>
          Discard edits
        </Button>
      </div>
    </div>
  );
}

function Editor({ dashboard, config }: { dashboard: DashboardDetail; config: AppConfig }) {
  const id = dashboard.id;
  const history = useHistory<Spec>(dashboard.spec);
  const spec = history.present;
  const [saved, setSaved] = useState<Spec>(dashboard.spec);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [message, setMessage] = useState("");
  const [state, setState] = useState<ViewState>(EMPTY_STATE);
  const save = useSaveDashboard(id);
  const publish = usePublishDashboard(id);
  const models = useModels();
  const theme = useChartTheme(spec.theme);
  const dirty = spec !== saved;
  const item = spec.items.find((i) => i.id === selected) ?? null;

  const datasets = useMemo(() => (models.data ?? []).filter((m) => m.hasData).map((m) => m.dataset), [models.data]);
  const usedDatasets = useMemo(
    () => [...new Set(spec.items.map((i) => i.dataset).filter((d): d is string => Boolean(d)))].sort(),
    [spec.items],
  );
  const fieldsByDataset = useFieldsFor(usedDatasets);
  const allFields = useMemo(() => {
    const seen = new Set<string>();
    return Object.values(fieldsByDataset).flat().filter((f) => !seen.has(f.ref) && seen.add(f.ref));
  }, [fieldsByDataset]);

  // Keyboard: Ctrl+Z, Ctrl+Y and Ctrl+Shift+Z, except while typing in a field.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest("input, textarea, select, .monaco-editor")) return;
      if (!(event.ctrlKey || event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key === "z" && !event.shiftKey) history.undo();
      else if (key === "y" || (key === "z" && event.shiftKey)) history.redo();
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [history]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const updateItem = (next: Item) => history.set({ ...spec, items: spec.items.map((i) => (i.id === next.id ? next : i)) });
  const addVisual = (manifest: Manifest) => {
    const size = manifest.defaultSize ?? { w: 6, h: 6 };
    const width = Math.min(size.w, spec.layout.columns);
    // Continue the last row if the new visual fits beside the previous one, else start a row.
    const last = spec.items.at(-1)?.position;
    const beside = last && last.x + last.w + width <= spec.layout.columns;
    const bottom = Math.max(0, ...spec.items.map((i) => i.position.y + i.position.h));
    const added: Item = {
      id: newId("v", [...spec.items.map((i) => i.id), ...(spec.filters ?? []).map((f) => f.id)]),
      visual: manifest.id,
      position: beside ? { x: last.x + last.w, y: last.y, w: width, h: size.h } : { x: 0, y: bottom, w: width, h: size.h },
      ...(Object.keys(manifest.roles).length ? { dataset: usedDatasets[0] ?? datasets[0], bindings: {} } : {}),
      config: defaultConfig(manifest),
    };
    history.set({ ...spec, items: [...spec.items, added] });
    setSelected(added.id);
    setTab("visual");
  };

  const saveProblems = save.error instanceof ApiError ? save.error.details.filter((d): d is Problem => Boolean(d.path)) : [];
  const conflict = save.error instanceof ApiError && save.error.code === "version_conflict";
  const tabs: [Tab, string][] = [["visual", "Visual"], ["filters", "Filters"], ["dashboard", "Dashboard"], ["history", "History"], ["json", "JSON"]];

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="mr-auto min-w-0">
          <p className="text-sm">
            <Link to="/dashboards" className="text-brand underline">Dashboards</Link>{" / "}
            <Link to={`/dashboards/${id}`} className="text-brand underline">View</Link>
          </p>
          <h1 className="truncate text-2xl font-semibold">{spec.title || "Untitled"}</h1>
        </div>
        <span role="status" className="text-xs text-zinc-500">
          {dirty ? "Unsaved changes" : `Saved as version ${dashboard.version}`}
        </span>
        {dashboard.publishedVersion === null ? <Badge>Not published</Badge> : dashboard.hasUnpublishedChanges ? <Badge tone="busy">Published: version {dashboard.publishedVersion}</Badge> : <Badge tone="good">Published</Badge>}
        <Button onClick={history.undo} disabled={!history.canUndo} title="Undo (Ctrl+Z)">Undo</Button>
        <Button onClick={history.redo} disabled={!history.canRedo} title="Redo (Ctrl+Y)">Redo</Button>
        {dirty && (
          <Input aria-label="Note for the version history (optional)" placeholder="Note (optional)" className="!w-40" maxLength={500} value={message} onChange={(e) => setMessage(e.target.value)} />
        )}
        <Button
          variant="primary"
          busy={save.isPending}
          disabled={!dirty}
          onClick={() =>
            save.mutate(
              { spec, baseVersion: dashboard.version, message: message || undefined },
              { onSuccess: () => { setSaved(spec); setMessage(""); } },
            )
          }
        >
          Save
        </Button>
        {dashboard.canPublish && (
          <Button
            busy={publish.isPending}
            disabled={dirty || !dashboard.hasUnpublishedChanges}
            title={dirty ? "Save first" : !dashboard.hasUnpublishedChanges ? "The latest version is already published" : "Make the saved version visible to viewers"}
            onClick={() => publish.mutate(true)}
          >
            Publish
          </Button>
        )}
      </div>
      <ProblemList problems={saveProblems} />
      {!saveProblems.length && <ErrorNote error={save.error ?? publish.error} />}
      {conflict && (
        <p className="mb-3 text-sm">
          <button type="button" className="text-brand underline" onClick={() => window.location.reload()}>
            Reload the latest version
          </button>{" "}
          (your unsaved changes will be lost).
        </p>
      )}

      <div className="flex flex-col gap-4 xl:flex-row">
        <div className="min-w-0 flex-1">
          <div className="mb-3 flex flex-wrap items-center gap-1.5" role="toolbar" aria-label="Add a visual">
            <span className="mr-1 text-sm text-zinc-600 dark:text-zinc-400">Add:</span>
            {visualList.map((manifest) => (
              <Button key={manifest.id} onClick={() => addVisual(manifest)} title={manifest.description}>
                {manifest.name}
              </Button>
            ))}
          </div>
          <FiltersBar dashboardId={id} spec={spec} source={{ kind: "inline", spec }} state={state} onState={setState} />
          {spec.items.length === 0 ? (
            <p className="rounded-lg border border-dashed border-zinc-300 p-10 text-center text-sm text-zinc-500 dark:border-zinc-700">
              Add a visual from the buttons above, then choose its fields in the panel.
            </p>
          ) : (
            <Canvas
              dashboardId={id}
              spec={spec}
              source={{ kind: "inline", spec }}
              state={state}
              onState={setState}
              theme={theme}
              calendar={config.calendar}
              editing={{
                selected,
                onSelect: (next) => { setSelected(next); if (next) setTab("visual"); },
                onLayout: (positions) =>
                  history.set((current) => ({
                    ...current,
                    items: current.items.map((i) => (positions[i.id] ? { ...i, position: positions[i.id]! } : i)),
                  })),
              }}
            />
          )}
        </div>

        <aside className="w-full shrink-0 rounded-lg border border-zinc-200 bg-white p-3 xl:w-96 dark:border-zinc-800 dark:bg-zinc-900" aria-label="Editor panel">
          <div role="tablist" className="mb-3 flex flex-wrap gap-1 border-b border-zinc-200 pb-2 dark:border-zinc-800">
            {tabs.map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                onClick={() => setTab(key)}
                className={`rounded-md px-2.5 py-1 text-sm ${tab === key ? "bg-zinc-200/70 font-medium dark:bg-zinc-800" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800/60"}`}
              >
                {label}
              </button>
            ))}
          </div>
          <div role="tabpanel" className={tab === "json" ? "h-[34rem]" : "max-h-[75vh] overflow-auto pr-1"}>
            {tab === "visual" &&
              (item ? (
                <ItemPanel
                  key={item.id}
                  item={item}
                  datasets={datasets}
                  fields={item.dataset ? (fieldsByDataset[item.dataset] ?? []) : []}
                  onChange={updateItem}
                  onRemove={() => { history.set({ ...spec, items: spec.items.filter((i) => i.id !== item.id) }); setSelected(null); }}
                />
              ) : (
                <p className="text-sm text-zinc-500">Select a visual on the canvas to configure it.</p>
              ))}
            {tab === "filters" && <FiltersPanel spec={spec} fields={allFields} onChange={(filters) => history.set({ ...spec, filters })} />}
            {tab === "dashboard" && <SettingsPanel spec={spec} onChange={history.set} />}
            {tab === "history" && (
              <HistoryPanel dashboardId={id} present={spec} dirty={dirty} onRestored={(restored) => { history.reset(restored); setSaved(restored); setSelected(null); }} />
            )}
            {tab === "json" && <JsonTab spec={spec} onApply={history.set} dark={theme.mode === "dark"} />}
          </div>
        </aside>
      </div>
    </div>
  );
}

export function DashboardEditor({ config }: { config: AppConfig }) {
  const { id = "" } = useParams();
  const dashboard = useDashboard(id);
  if (dashboard.isPending) return <p className="text-sm text-zinc-500">Loading...</p>;
  if (dashboard.isError) {
    return (
      <div className="mx-auto max-w-5xl">
        <ErrorNote error={dashboard.error} />
        <Link to="/dashboards" className="mt-3 inline-block text-sm text-brand underline">Back to dashboards</Link>
      </div>
    );
  }
  if (!dashboard.data.canEdit) return <ErrorNote error={new Error("You do not have permission to edit dashboards.")} />;
  // The editor keeps its own working copy, so it is keyed by id, not by version.
  return <Editor key={id} dashboard={dashboard.data} config={config} />;
}
