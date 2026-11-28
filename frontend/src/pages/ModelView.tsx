// The Model view (V2_DESIGN 1.3): the workspace model as a canvas of tables and the
// relationships between them, the same model as YAML, and its history. Every view
// saves the same document, and the canvas is drawn from it.
import { Suspense, lazy, useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { DateTablePanel, SuggestionsPanel } from "../components/model/HelpPanels";
import { ModelHistory } from "../components/model/ModelHistory";
import { ModelYaml } from "../components/model/ModelYaml";
import { type Pair, RelationshipPanel } from "../components/model/RelationshipPanel";
import { TablePanel } from "../components/model/TablePanel";
import { ProblemList, WarningList } from "../components/model/lists";
import { Button, Card, ErrorNote, PageHeader } from "../components/ui";
import type { Me } from "../lib/config";
import { complete, describeRelationship, useWorkspaceModel } from "../lib/workspaceModel";

const ModelCanvas = lazy(() => import("../components/model/ModelCanvas"));

type Tab = "canvas" | "yaml" | "history";
const TABS: Record<Tab, string> = { canvas: "Canvas", yaml: "YAML", history: "History" };

type Open =
  | { kind: "new"; pair: Pair | null }
  | { kind: "relationship"; id: string }
  | { kind: "table"; name: string }
  | { kind: "suggestions" }
  | { kind: "date-table" }
  | null;

export function ModelView({ me }: { me: Me }) {
  const model = useWorkspaceModel();
  const [tab, setTab] = useState<Tab>("canvas");
  const [yamlOpened, setYamlOpened] = useState(false);
  const [open, setOpen] = useState<Open>(null);
  const close = useCallback(() => setOpen(null), []);
  const onPair = useCallback((pair: Pair) => setOpen({ kind: "new", pair }), []);
  const onTable = useCallback((name: string) => setOpen({ kind: "table", name }), []);
  const onRelationship = useCallback((id: string) => setOpen({ kind: "relationship", id }), []);
  const canMakeDatasets = me.permissions.includes("datasets:edit");

  const show = (next: Tab) => {
    setTab(next);
    if (next === "yaml") setYamlOpened(true);
  };

  if (model.isPending) return <p className="text-sm text-zinc-500">Loading the model.</p>;
  if (model.error) return <ErrorNote error={model.error} />;
  const data = model.data;
  const relationships = Object.entries(data.document.relationships);
  const tableCount = Object.keys(data.document.tables).length;

  return (
    <div>
      <PageHeader
        title="Model"
        description={
          <>
            The tables dashboards and the assistant use, and the relationships between them. Drag a field onto a field
            of another table to relate the tables. Version {data.version}.
          </>
        }
        actions={
          tab === "canvas" && (
            <>
              <Button disabled={tableCount < 2} onClick={() => setOpen({ kind: "new", pair: null })}>
                Add a relationship
              </Button>
              <Button disabled={tableCount < 2} onClick={() => setOpen({ kind: "suggestions" })}>
                Suggest relationships
              </Button>
              {canMakeDatasets && (
                <Button onClick={() => setOpen({ kind: "date-table" })}>Generate a date table</Button>
              )}
            </>
          )
        }
      />
      <div role="tablist" className="mb-4 flex gap-1">
        {(Object.keys(TABS) as Tab[]).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => show(key)}
            className={`rounded-md px-3 py-1.5 text-sm ${tab === key ? "bg-zinc-200/70 font-medium dark:bg-zinc-800" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400"}`}
          >
            {TABS[key]}
          </button>
        ))}
      </div>

      {tab === "canvas" && (
        <div role="tabpanel" aria-label="Canvas" className="space-y-4">
          <div className="flex flex-col gap-4 lg:h-[72vh] lg:min-h-[30rem] lg:flex-row">
            <div className="h-[60vh] min-h-[24rem] min-w-0 flex-1 overflow-hidden rounded-lg border border-zinc-200 bg-white lg:h-auto dark:border-zinc-800 dark:bg-zinc-900">
              <Suspense fallback={<p className="p-4 text-sm text-zinc-500">Loading the canvas.</p>}>
                <ModelCanvas
                  model={data}
                  editable
                  selectedTable={open?.kind === "table" ? open.name : null}
                  selectedRelationship={open?.kind === "relationship" ? open.id : null}
                  onPair={onPair}
                  onTable={onTable}
                  onRelationship={onRelationship}
                  onBackground={close}
                />
              </Suspense>
            </div>
            {open?.kind === "new" && (
              <RelationshipPanel
                model={data}
                target={{ kind: "new", pair: open.pair }}
                onClose={close}
                onSaved={(id) => setOpen(id ? { kind: "relationship", id } : null)}
              />
            )}
            {open?.kind === "relationship" && (
              <RelationshipPanel
                model={data}
                target={{ kind: "existing", id: open.id }}
                onClose={close}
                onSaved={(id) => setOpen(id ? { kind: "relationship", id } : null)}
              />
            )}
            {open?.kind === "table" && <TablePanel model={data} name={open.name} onClose={close} />}
            {open?.kind === "suggestions" && <SuggestionsPanel model={data} onClose={close} />}
            {open?.kind === "date-table" && <DateTablePanel model={data} onClose={close} />}
          </div>

          <ProblemList label="Problems with the model" problems={data.problems} />
          <WarningList label="Warnings about the model" warnings={data.warnings} />

          <Card className="p-4">
            <h2 className="mb-2 text-base font-semibold">Relationships</h2>
            {relationships.length === 0 ? (
              <p className="text-sm text-zinc-600 dark:text-zinc-400">
                None yet. Drag a field onto a field of another table, or use Suggest relationships.
              </p>
            ) : (
              <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {relationships.map(([id, rel]) => {
                  const full = complete(rel);
                  return (
                    <li key={id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                      <span className="font-mono text-xs text-zinc-500">{id}</span>
                      <span>{describeRelationship(rel)}</span>
                      <span className="text-xs text-zinc-500">
                        {full.cardinality === "one_to_one" ? "one to one" : "many to one"}
                        {full.crossFilter === "both" ? ", filters both ways" : ""}
                        {full.securityFilter === "both" ? ", security both ways" : ""}
                        {full.active ? "" : ", inactive"}
                        {full.joinType === "inner" ? ", inner join" : ""}
                      </span>
                      <Button
                        variant="ghost"
                        className="ml-auto"
                        aria-label={`Edit relationship ${id}`}
                        onClick={() => setOpen({ kind: "relationship", id })}
                      >
                        Edit
                      </Button>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Each table also has a page of its own, to try queries and import model files:{" "}
            <Link to="/models" className="text-brand underline">
              tables
            </Link>
            .
          </p>
        </div>
      )}
      {yamlOpened && (
        <div role="tabpanel" aria-label="YAML" hidden={tab !== "yaml"}>
          <ModelYaml model={data} />
        </div>
      )}
      {tab === "history" && (
        <div role="tabpanel" aria-label="History">
          <ModelHistory />
        </div>
      )}
    </div>
  );
}
