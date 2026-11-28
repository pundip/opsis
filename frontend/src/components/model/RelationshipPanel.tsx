// One relationship in the side panel: a new one, from two fields dragged together or
// chosen here, whose keys are checked before it is added; or one the model has.
import { useCallback, useEffect, useState } from "react";
import {
  type FullRelationship,
  type KeyCheck,
  type RelationshipEnd,
  type WorkspaceModel,
  complete,
  pointer,
  useKeyCheck,
  usePatchModel,
} from "../../lib/workspaceModel";
import { Badge, Button, ErrorNote, Field, Input, Select } from "../ui";
import { ProblemList, SaveError, SidePanel, WarningList } from "./lists";

export interface Pair {
  from: RelationshipEnd;
  to: RelationshipEnd;
}

export type RelationshipTarget = { kind: "new"; pair: Pair | null } | { kind: "existing"; id: string };

const MAX_COLUMNS = 8;

function columnsOf(model: WorkspaceModel, table: string): string[] {
  const info = model.tables.find((t) => t.name === table);
  if (info?.hasData) return info.columns.map((column) => column.name);
  const dimensions = Object.values(model.document.tables[table]?.dimensions ?? {});
  return [...new Set(dimensions.flatMap((dimension) => (dimension.column ? [dimension.column] : [])))];
}

function labelOf(model: WorkspaceModel, table: string): string {
  return model.document.tables[table]?.label || model.tables.find((t) => t.name === table)?.label || table;
}

const samePair = (a: Pair, b: Pair) => JSON.stringify([a.from, a.to]) === JSON.stringify([b.from, b.to]);

/** The tables and the columns they join on, as pairs of columns. */
function Ends({
  model,
  pair,
  tablesFixed,
  onChange,
}: {
  model: WorkspaceModel;
  pair: Pair;
  tablesFixed: boolean;
  onChange: (pair: Pair) => void;
}) {
  const tables = Object.keys(model.document.tables);
  const fromColumns = columnsOf(model, pair.from.table);
  const toColumns = columnsOf(model, pair.to.table);
  const count = Math.max(pair.from.columns.length, pair.to.columns.length);
  const setTable = (side: "from" | "to", table: string) =>
    onChange({ ...pair, [side]: { table, columns: [columnsOf(model, table)[0] ?? ""] } });
  const setColumn = (side: "from" | "to", index: number, column: string) => {
    const columns = [...pair[side].columns];
    columns[index] = column;
    onChange({ ...pair, [side]: { ...pair[side], columns } });
  };
  const removeAt = (index: number) =>
    onChange({
      from: { ...pair.from, columns: pair.from.columns.filter((_, i) => i !== index) },
      to: { ...pair.to, columns: pair.to.columns.filter((_, i) => i !== index) },
    });
  const add = () =>
    onChange({
      from: { ...pair.from, columns: [...pair.from.columns, fromColumns[0] ?? ""] },
      to: { ...pair.to, columns: [...pair.to.columns, toColumns[0] ?? ""] },
    });
  return (
    <fieldset className="space-y-2">
      <legend className="mb-1 text-sm font-medium">Fields</legend>
      {!tablesFixed && (
        <div className="grid grid-cols-2 gap-2">
          <Select aria-label="First table" value={pair.from.table} onChange={(e) => setTable("from", e.target.value)}>
            {tables.map((name) => (
              <option key={name} value={name}>
                {labelOf(model, name)}
              </option>
            ))}
          </Select>
          <Select aria-label="Second table" value={pair.to.table} onChange={(e) => setTable("to", e.target.value)}>
            {tables.map((name) => (
              <option key={name} value={name}>
                {labelOf(model, name)}
              </option>
            ))}
          </Select>
        </div>
      )}
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="flex items-center gap-2">
          <Select
            aria-label={`${pair.from.table} field ${index + 1}`}
            value={pair.from.columns[index] ?? ""}
            onChange={(e) => setColumn("from", index, e.target.value)}
          >
            {fromColumns.map((column) => (
              <option key={column} value={column}>
                {pair.from.table}.{column}
              </option>
            ))}
          </Select>
          <span aria-hidden className="text-zinc-500">
            =
          </span>
          <Select
            aria-label={`${pair.to.table} field ${index + 1}`}
            value={pair.to.columns[index] ?? ""}
            onChange={(e) => setColumn("to", index, e.target.value)}
          >
            {toColumns.map((column) => (
              <option key={column} value={column}>
                {pair.to.table}.{column}
              </option>
            ))}
          </Select>
          {count > 1 && (
            <Button variant="ghost" aria-label={`Remove field pair ${index + 1}`} onClick={() => removeAt(index)}>
              Remove
            </Button>
          )}
        </div>
      ))}
      {count < MAX_COLUMNS && (
        <Button variant="ghost" onClick={add} title="For keys of more than one field">
          Add a field pair
        </Button>
      )}
    </fieldset>
  );
}

/** Cardinality, filter directions, active and the join. */
function Settings({
  model,
  rel,
  onChange,
}: {
  model: WorkspaceModel;
  rel: FullRelationship;
  onChange: (rel: FullRelationship) => void;
}) {
  const many = labelOf(model, rel.from.table);
  const one = labelOf(model, rel.to.table);
  return (
    <div className="space-y-3">
      <Field label="Cardinality" htmlFor="rel-cardinality">
        <Select
          id="rel-cardinality"
          value={rel.cardinality}
          onChange={(e) => onChange({ ...rel, cardinality: e.target.value as FullRelationship["cardinality"] })}
        >
          <option value="many_to_one">
            Many to one: many {many} rows to one {one} row
          </option>
          <option value="one_to_one">One to one</option>
        </Select>
      </Field>
      <Field label="Filters" htmlFor="rel-cross">
        <Select
          id="rel-cross"
          value={rel.crossFilter}
          onChange={(e) => {
            const crossFilter = e.target.value as FullRelationship["crossFilter"];
            // Security can travel both ways only when filters do.
            onChange({ ...rel, crossFilter, securityFilter: crossFilter === "single" ? "single" : rel.securityFilter });
          }}
        >
          <option value="single">
            One way: filters on {one} filter {many}
          </option>
          <option value="both">Both ways: filters on {many} also filter {one}</option>
        </Select>
      </Field>
      <Field
        label="Row-level security"
        htmlFor="rel-security"
        hint="Rules on a table hide the rows they hide everywhere the relationship carries them."
      >
        <Select
          id="rel-security"
          value={rel.securityFilter}
          onChange={(e) => onChange({ ...rel, securityFilter: e.target.value as FullRelationship["securityFilter"] })}
        >
          <option value="single">
            One way: rules on {one} filter {many}
          </option>
          <option value="both" disabled={rel.crossFilter !== "both"}>
            Both ways (needs filters both ways)
          </option>
        </Select>
      </Field>
      <Field label="Rows with no match" htmlFor="rel-join">
        <Select
          id="rel-join"
          value={rel.joinType}
          onChange={(e) => onChange({ ...rel, joinType: e.target.value as FullRelationship["joinType"] })}
        >
          <option value="left">
            Keep {many} rows with no {one} row (their {one} fields are empty)
          </option>
          <option value="inner">Leave out {many} rows with no {one} row</option>
        </Select>
      </Field>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={rel.active}
          onChange={(e) => onChange({ ...rel, active: e.target.checked })}
        />
        <span>
          Active
          <span className="block text-xs text-zinc-500">
            Queries use active relationships. An inactive one (drawn dashed) is used only by measures that name it in{" "}
            <code>relationship</code>.
          </span>
        </span>
      </label>
      <Field label="Description" htmlFor="rel-description">
        <Input
          id="rel-description"
          maxLength={1000}
          value={rel.description ?? ""}
          onChange={(e) => onChange({ ...rel, description: e.target.value })}
        />
      </Field>
    </div>
  );
}

function stored(rel: FullRelationship): FullRelationship {
  const { description, ...rest } = rel;
  return description?.trim() ? { ...rest, description: description.trim() } : rest;
}

function Evidence({ check }: { check: KeyCheck }) {
  const { from, to } = check.keys;
  return (
    <div className="space-y-2 text-sm">
      <p>
        Confidence <Badge tone={check.confidence >= 0.8 ? "good" : "neutral"}>{Math.round(check.confidence * 100)}%</Badge>
      </p>
      <ul className="list-disc space-y-1 pl-5 text-zinc-700 dark:text-zinc-300">
        {check.evidence.map((line) => (
          <li key={line}>{line}</li>
        ))}
        <li>
          {to.table}: {to.distinctKeys.toLocaleString("en-AU")} different keys in {to.rows.toLocaleString("en-AU")} rows
          {to.unique ? ", each once" : ""}. {from.table}: {from.distinctKeys.toLocaleString("en-AU")} different keys in{" "}
          {from.rows.toLocaleString("en-AU")} rows{from.emptyKeys ? `, ${from.emptyKeys.toLocaleString("en-AU")} empty` : ""}.
        </li>
      </ul>
      {check.warnings.length > 0 && (
        <ul
          aria-label="Warnings about these keys"
          className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3 wrap-anywhere text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
        >
          {check.warnings.map((warning) => (
            <li key={warning.code + warning.message}>{warning.message}</li>
          ))}
        </ul>
      )}
      {check.inactiveBecause && (
        <p className="rounded-md border border-zinc-200 bg-zinc-50 p-2 text-xs text-zinc-700 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300">
          {check.inactiveBecause}
        </p>
      )}
    </div>
  );
}

function NewRelationship({ model, start, onClose, onSaved }: { model: WorkspaceModel; start: Pair | null; onClose: () => void; onSaved: (id: string) => void }) {
  const tables = Object.keys(model.document.tables);
  const first: Pair = start ?? {
    from: { table: tables[0] ?? "", columns: [columnsOf(model, tables[0] ?? "")[0] ?? ""] },
    to: { table: tables[1] ?? "", columns: [columnsOf(model, tables[1] ?? "")[0] ?? ""] },
  };
  const [pair, setPair] = useState<Pair>(first);
  const [result, setResult] = useState<{ check: KeyCheck; pair: Pair } | null>(null);
  const [draft, setDraft] = useState<FullRelationship | null>(null);
  const check = useKeyCheck();
  const patch = usePatchModel();
  const { mutate: runCheck } = check;
  // The one side is the side whose key is unique: the check may swap the sides.
  const accept = useCallback((found: KeyCheck) => {
    const checkedPair = { from: found.relationship.from, to: found.relationship.to };
    setPair(checkedPair);
    setResult({ check: found, pair: checkedPair });
    setDraft(found.relationship);
  }, []);
  const checked = result && samePair(result.pair, pair) ? result.check : null;

  useEffect(() => {
    if (start) runCheck(start, { onSuccess: accept });
  }, [start, runCheck, accept]);

  const checkKeys = () => runCheck(pair, { onSuccess: accept });

  return (
    <SidePanel title="New relationship" onClose={onClose}>
      <Ends model={model} pair={pair} tablesFixed={start !== null} onChange={(next) => setPair(next)} />
      {!checked && (
        <Button variant={check.isPending ? "secondary" : "primary"} busy={check.isPending} onClick={checkKeys}>
          {result ? "Check again" : "Check the keys"}
        </Button>
      )}
      <ErrorNote error={check.error} />
      {checked && draft && (
        <>
          <p className="text-sm">
            <span className="font-medium">{labelOf(model, checked.relationship.from.table)}</span> is the many side and{" "}
            <span className="font-medium">{labelOf(model, checked.relationship.to.table)}</span> the one side
            {checked.swapped ? " (swapped: the second key is the unique one)" : ""}.
          </p>
          <Evidence check={checked} />
          <Settings model={model} rel={draft} onChange={setDraft} />
          <p className="text-xs text-zinc-500">{checked.note} It is saved as {checked.id}.</p>
          <SaveError error={patch.error} />
          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              busy={patch.isPending}
              onClick={() =>
                patch.mutate(
                  {
                    ops: [{ op: "add", path: pointer("relationships", checked.id), value: stored(draft) }],
                    baseVersion: model.version,
                    message: `Relationship ${checked.id}`,
                  },
                  { onSuccess: () => onSaved(checked.id) },
                )
              }
            >
              Add relationship
            </Button>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
          </div>
        </>
      )}
    </SidePanel>
  );
}

function ExistingRelationship({ model, id, onClose, onSaved }: { model: WorkspaceModel; id: string; onClose: () => void; onSaved: (id: string | null) => void }) {
  const saved = model.document.relationships[id];
  const [draft, setDraft] = useState<FullRelationship | null>(saved ? complete(saved) : null);
  const patch = usePatchModel();
  if (!saved || !draft) {
    return (
      <SidePanel title="Relationship" onClose={onClose}>
        <p className="text-sm">There is no relationship called {id} now.</p>
      </SidePanel>
    );
  }
  const warnings = model.warnings.filter(
    (warning) => warning.path === `/relationships/${id}` || warning.path.startsWith(`/relationships/${id}/`),
  );
  const problems = model.problems.filter(
    (problem) => problem.path === `/relationships/${id}` || problem.path.startsWith(`/relationships/${id}/`),
  );
  const changed = JSON.stringify(stored(draft)) !== JSON.stringify(stored(complete(saved)));
  return (
    <SidePanel title={`Relationship ${id}`} onClose={onClose}>
      <Ends
        model={model}
        pair={{ from: draft.from, to: draft.to }}
        tablesFixed
        onChange={(pair) => setDraft({ ...draft, ...pair })}
      />
      <Settings model={model} rel={draft} onChange={setDraft} />
      <ProblemList label="Problems with this relationship" problems={problems} />
      <WarningList label="Warnings about this relationship" warnings={warnings} />
      <SaveError error={patch.error} />
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          disabled={!changed}
          busy={patch.isPending && patch.variables?.ops[0]?.op === "replace"}
          onClick={() =>
            patch.mutate(
              {
                ops: [{ op: "replace", path: pointer("relationships", id), value: stored(draft) }],
                baseVersion: model.version,
                message: `Relationship ${id}`,
              },
              { onSuccess: () => onSaved(id) },
            )
          }
        >
          Save relationship
        </Button>
        <Button variant="ghost" disabled={!changed} onClick={() => setDraft(complete(saved))}>
          Discard changes
        </Button>
        <Button
          variant="danger"
          className="ml-auto"
          busy={patch.isPending && patch.variables?.ops[0]?.op === "remove"}
          onClick={() => {
            if (!window.confirm(`Remove the relationship ${id}? Measures that name it stop working.`)) return;
            patch.mutate(
              { ops: [{ op: "remove", path: pointer("relationships", id) }], baseVersion: model.version, message: `Removed relationship ${id}` },
              { onSuccess: () => onSaved(null) },
            );
          }}
        >
          Remove
        </Button>
      </div>
    </SidePanel>
  );
}

export function RelationshipPanel({
  model,
  target,
  onClose,
  onSaved,
}: {
  model: WorkspaceModel;
  target: RelationshipTarget;
  onClose: () => void;
  onSaved: (id: string | null) => void;
}) {
  if (target.kind === "existing") {
    // Drawn afresh for each version, so a saved change shows as saved.
    return <ExistingRelationship key={`${target.id}@${model.version}`} model={model} id={target.id} onClose={onClose} onSaved={onSaved} />;
  }
  const key = target.pair ? JSON.stringify(target.pair) : "chosen";
  return <NewRelationship key={key} model={model} start={target.pair} onClose={onClose} onSaved={onSaved} />;
}
