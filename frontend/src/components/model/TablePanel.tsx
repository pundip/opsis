// One table in the side panel: its label, description and visibility, the date table
// mark, how its fields show, and its measures. Saved as one change to the table.
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  DATA_CATEGORIES,
  type DimensionEntry,
  type MeasureEntry,
  type TableEntry,
  type WorkspaceModel,
  pointer,
  usePatchModel,
} from "../../lib/workspaceModel";
import { Button, Field, Input, Select } from "../ui";
import { ProblemList, SaveError, SidePanel, WarningList } from "./lists";

const AGGREGATIONS: [NonNullable<MeasureEntry["agg"]>, string][] = [
  ["sum", "Sum"],
  ["count", "Count"],
  ["count_distinct", "Count of different values"],
  ["avg", "Average"],
  ["min", "Lowest"],
  ["max", "Highest"],
];
const FORMATS = ["number", "percent", "currency_aud", "currency_nzd", "currency_usd", "currency_gbp", "currency_eur"];
const NAME = /^[a-z_][a-z0-9_]{0,62}$/;

/** Sets a property, or leaves it out when it is empty or false. */
function set<T extends Record<string, unknown>>(entry: T, key: string, value: unknown): T {
  const next: Record<string, unknown> = { ...entry };
  if (value === "" || value === false || value === undefined || value === null) delete next[key];
  else next[key] = value;
  return next as T;
}

function FieldSettings({
  name,
  dimension,
  onChange,
}: {
  name: string;
  dimension: DimensionEntry;
  onChange: (dimension: DimensionEntry) => void;
}) {
  const id = `dim-${name}`;
  return (
    <div className="space-y-3 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
      <p className="text-xs text-zinc-500">
        {dimension.column ? (
          <>
            Column <code>{dimension.column}</code>
          </>
        ) : (
          <>
            Expression <code>{dimension.expr}</code>
          </>
        )}
        , {dimension.type}
      </p>
      <Field label="Label" htmlFor={`${id}-label`}>
        <Input
          id={`${id}-label`}
          maxLength={200}
          value={dimension.label ?? ""}
          onChange={(e) => onChange(set(dimension, "label", e.target.value))}
        />
      </Field>
      <Field label="Description" htmlFor={`${id}-description`}>
        <Input
          id={`${id}-description`}
          maxLength={1000}
          value={dimension.description ?? ""}
          onChange={(e) => onChange(set(dimension, "description", e.target.value))}
        />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Format" htmlFor={`${id}-format`}>
          <Input
            id={`${id}-format`}
            list="model-formats"
            maxLength={40}
            value={dimension.format ?? ""}
            onChange={(e) => onChange(set(dimension, "format", e.target.value))}
          />
        </Field>
        <Field label="Data category" htmlFor={`${id}-category`}>
          <Select
            id={`${id}-category`}
            value={dimension.dataCategory ?? ""}
            onChange={(e) => onChange(set(dimension, "dataCategory", e.target.value))}
          >
            <option value="">None</option>
            {DATA_CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={dimension.hidden ?? false}
          onChange={(e) => onChange(set(dimension, "hidden", e.target.checked))}
        />
        Hidden from field lists
      </label>
    </div>
  );
}

function MeasureForm({
  name,
  measure,
  columns,
  relationships,
  onChange,
  onRemove,
}: {
  name: string;
  measure: MeasureEntry;
  columns: string[];
  relationships: string[];
  onChange: (measure: MeasureEntry) => void;
  onRemove: () => void;
}) {
  const id = `measure-${name}`;
  const isExpression = measure.expr !== undefined;
  return (
    <div className="space-y-3 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
      <fieldset className="flex flex-wrap gap-4 text-sm">
        <legend className="sr-only">Kind of measure</legend>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name={`${id}-kind`}
            checked={!isExpression}
            onChange={() => onChange({ label: measure.label, format: measure.format, description: measure.description, agg: "count" })}
          />
          An aggregation
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name={`${id}-kind`}
            checked={isExpression}
            onChange={() => onChange({ label: measure.label, format: measure.format, description: measure.description, expr: "" })}
          />
          Arithmetic over measures
        </label>
      </fieldset>
      {isExpression ? (
        <Field
          label="Expression"
          htmlFor={`${id}-expr`}
          hint="Other measures in braces: {total_amount} / NULLIF({gift_count}, 0). Another table's: {targets.target_amount}."
        >
          <Input id={`${id}-expr`} maxLength={2000} value={measure.expr ?? ""} onChange={(e) => onChange({ ...measure, expr: e.target.value })} />
        </Field>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Aggregation" htmlFor={`${id}-agg`}>
              <Select id={`${id}-agg`} value={measure.agg ?? "count"} onChange={(e) => onChange({ ...measure, agg: e.target.value as MeasureEntry["agg"] })}>
                {AGGREGATIONS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Field" htmlFor={`${id}-column`}>
              <Select id={`${id}-column`} value={measure.column ?? ""} onChange={(e) => onChange(set(measure, "column", e.target.value))}>
                <option value="">{measure.agg === "count" ? "Rows" : "Choose a field"}</option>
                {columns.map((column) => (
                  <option key={column} value={column}>
                    {column}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label="Only rows where (optional)" htmlFor={`${id}-filter`} hint="For example status = 'Paid', or donors.tier = 'Major'.">
            <Input id={`${id}-filter`} maxLength={2000} value={measure.filter ?? ""} onChange={(e) => onChange(set(measure, "filter", e.target.value))} />
          </Field>
          {relationships.length > 0 && (
            <Field label="Through relationship" htmlFor={`${id}-relationship`} hint="Usually an inactive one, used instead of the active path.">
              <Select id={`${id}-relationship`} value={measure.relationship ?? ""} onChange={(e) => onChange(set(measure, "relationship", e.target.value))}>
                <option value="">The active path</option>
                {relationships.map((rid) => (
                  <option key={rid} value={rid}>
                    {rid}
                  </option>
                ))}
              </Select>
            </Field>
          )}
        </>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Label" htmlFor={`${id}-label`}>
          <Input id={`${id}-label`} maxLength={200} value={measure.label ?? ""} onChange={(e) => onChange(set(measure, "label", e.target.value))} />
        </Field>
        <Field label="Format" htmlFor={`${id}-format`}>
          <Input id={`${id}-format`} list="model-formats" maxLength={40} value={measure.format ?? ""} onChange={(e) => onChange(set(measure, "format", e.target.value))} />
        </Field>
      </div>
      <Field label="Description" htmlFor={`${id}-description`}>
        <Input id={`${id}-description`} maxLength={1000} value={measure.description ?? ""} onChange={(e) => onChange(set(measure, "description", e.target.value))} />
      </Field>
      <div className="flex items-center gap-2">
        <label className="mr-auto flex items-center gap-2 text-sm">
          <input type="checkbox" checked={measure.hidden ?? false} onChange={(e) => onChange(set(measure, "hidden", e.target.checked))} />
          Hidden from field lists
        </label>
        <Button variant="danger" onClick={onRemove} aria-label={`Remove measure ${name}`}>
          Remove
        </Button>
      </div>
    </div>
  );
}

function TableForm({
  model,
  name,
  saved,
  onClose,
}: {
  model: WorkspaceModel;
  name: string;
  saved: TableEntry;
  onClose: () => void;
}) {
  const info = model.tables.find((table) => table.name === name);
  const [draft, setDraft] = useState<TableEntry>(saved);
  const [field, setField] = useState<string>(Object.keys(saved.dimensions ?? {})[0] ?? "");
  const [open, setOpen] = useState<string | null>(null);
  const [newMeasure, setNewMeasure] = useState("");
  const patch = usePatchModel();
  const columns = info?.hasData
    ? info.columns.map((column) => column.name)
    : [...new Set(Object.values(saved.dimensions ?? {}).flatMap((d) => (d.column ? [d.column] : [])))];
  const dateColumns = info?.columns.filter((column) => column.type === "date").map((column) => column.name) ?? [];
  const relationships = Object.entries(model.document.relationships)
    .filter(([, rel]) => rel.from.table === name || rel.to.table === name)
    .map(([rid]) => rid);
  const dimensions = draft.dimensions ?? {};
  const measures = draft.measures ?? {};
  const changed = JSON.stringify(draft) !== JSON.stringify(saved);
  const inTable = (path: string) => path === `/tables/${name}` || path.startsWith(`/tables/${name}/`);
  const nameProblem =
    newMeasure && !NAME.test(newMeasure)
      ? "Lower-case letters, digits and underscores, starting with a letter."
      : newMeasure in measures || newMeasure in dimensions
        ? "The table already has a field with this name."
        : undefined;

  return (
    <SidePanel title={`Table ${draft.label || info?.label || name}`} onClose={onClose}>
      <datalist id="model-formats">
        {FORMATS.map((format) => (
          <option key={format} value={format} />
        ))}
      </datalist>
      <p className="text-xs text-zinc-500">
        <code>{name}</code>
        {info?.datasetId && (
          <>
            {" "}
            from the dataset{" "}
            <Link to={`/datasets/${info.datasetId}`} className="text-brand underline">
              {info.label}
            </Link>
          </>
        )}
        . <Link to={`/models/${name}`} className="text-brand underline">Try queries on this table</Link>.
      </p>
      <ProblemList label="Problems with this table" problems={model.problems.filter((p) => inTable(p.path))} />
      <WarningList label="Warnings about this table" warnings={model.warnings.filter((w) => inTable(w.path))} />
      <Field label="Label" htmlFor="table-label">
        <Input id="table-label" maxLength={200} value={draft.label ?? ""} onChange={(e) => setDraft(set(draft, "label", e.target.value))} />
      </Field>
      <Field label="Description" htmlFor="table-description">
        <Input id="table-description" maxLength={2000} value={draft.description ?? ""} onChange={(e) => setDraft(set(draft, "description", e.target.value))} />
      </Field>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-0.5" checked={draft.hidden ?? false} onChange={(e) => setDraft(set(draft, "hidden", e.target.checked))} />
        <span>
          Hidden from field lists
          <span className="block text-xs text-zinc-500">Still used through its relationships.</span>
        </span>
      </label>
      {(dateColumns.length > 0 || draft.dateTable) && (
        <Field label="Date table" htmlFor="table-date" hint="One row per day, no gaps, the date unique. Time intelligence uses it.">
          <Select
            id="table-date"
            value={draft.dateTable?.column ?? ""}
            onChange={(e) => setDraft(set(draft, "dateTable", e.target.value ? { column: e.target.value } : undefined))}
          >
            <option value="">Not the date table</option>
            {dateColumns.map((column) => (
              <option key={column} value={column}>
                The date table, by {column}
              </option>
            ))}
          </Select>
        </Field>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Fields</h3>
        {Object.keys(dimensions).length === 0 ? (
          <p className="text-sm text-zinc-500">No fields.</p>
        ) : (
          <>
            <Select aria-label="Field to change" value={field} onChange={(e) => setField(e.target.value)}>
              {Object.entries(dimensions).map(([key, dimension]) => (
                <option key={key} value={key}>
                  {dimension.label ? `${dimension.label} (${key})` : key}
                  {dimension.hidden ? ", hidden" : ""}
                </option>
              ))}
            </Select>
            {dimensions[field] && (
              <FieldSettings
                key={field}
                name={field}
                dimension={dimensions[field]}
                onChange={(dimension) => setDraft({ ...draft, dimensions: { ...dimensions, [field]: dimension } })}
              />
            )}
          </>
        )}
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Measures</h3>
        <ul className="divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
          {Object.entries(measures).map(([key, measure]) => (
            <li key={key} className="p-2">
              <button
                type="button"
                aria-expanded={open === key}
                className="flex w-full items-center gap-2 text-left text-sm"
                onClick={() => setOpen(open === key ? null : key)}
              >
                <span className="font-medium">{measure.label || key}</span>
                <code className="truncate text-xs text-zinc-500">
                  {measure.expr !== undefined ? measure.expr : `${measure.agg ?? "count"}(${measure.column ?? "rows"})`}
                </code>
              </button>
              {open === key && (
                <div className="mt-2">
                  <MeasureForm
                    name={key}
                    measure={measure}
                    columns={columns}
                    relationships={relationships}
                    onChange={(next) => setDraft({ ...draft, measures: { ...measures, [key]: next } })}
                    onRemove={() => {
                      const rest = { ...measures };
                      delete rest[key];
                      setDraft({ ...draft, measures: rest });
                      setOpen(null);
                    }}
                  />
                </div>
              )}
            </li>
          ))}
          {Object.keys(measures).length === 0 && <li className="p-2 text-sm text-zinc-500">No measures yet.</li>}
        </ul>
        <div className="flex items-start gap-2">
          <div className="flex-1">
            <Field label="New measure" htmlFor="new-measure" error={nameProblem} hint="Its name in queries, for example total_amount.">
              <Input id="new-measure" maxLength={63} value={newMeasure} onChange={(e) => setNewMeasure(e.target.value.trim())} />
            </Field>
          </div>
          <Button
            className="mt-6"
            disabled={!newMeasure || nameProblem !== undefined}
            onClick={() => {
              setDraft({ ...draft, measures: { ...measures, [newMeasure]: { agg: "count" } } });
              setOpen(newMeasure);
              setNewMeasure("");
            }}
          >
            Add
          </Button>
        </div>
      </section>

      <SaveError error={patch.error} />
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          disabled={!changed}
          busy={patch.isPending}
          onClick={() =>
            patch.mutate({
              ops: [{ op: "replace", path: pointer("tables", name), value: draft }],
              baseVersion: model.version,
              message: `Table ${name}`,
            })
          }
        >
          Save table
        </Button>
        <Button variant="ghost" disabled={!changed} onClick={() => setDraft(saved)}>
          Discard changes
        </Button>
      </div>
    </SidePanel>
  );
}

export function TablePanel({ model, name, onClose }: { model: WorkspaceModel; name: string; onClose: () => void }) {
  const saved = model.document.tables[name];
  if (!saved) {
    return (
      <SidePanel title="Table" onClose={onClose}>
        <p className="text-sm">There is no table called {name} now.</p>
      </SidePanel>
    );
  }
  // Drawn afresh for each version, so a saved change shows as saved.
  return <TableForm key={`${name}@${model.version}`} model={model} name={name} saved={saved} onClose={onClose} />;
}
