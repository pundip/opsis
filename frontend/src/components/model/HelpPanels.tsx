// Help with relationships: suggestions read from the data, and a generated date
// table. Both only propose: nothing is related until the person adds it.
import { useEffect, useState } from "react";
import {
  type Proposal,
  type WorkspaceModel,
  describeRelationship,
  pointer,
  useGenerateDateTable,
  usePatchModel,
  useSuggestions,
} from "../../lib/workspaceModel";
import { Badge, Button, ErrorNote, Field, Input } from "../ui";
import { SaveError, SidePanel } from "./lists";

function ProposalList({
  model,
  proposals,
  onAdded,
}: {
  model: WorkspaceModel;
  proposals: Proposal[];
  onAdded: (ids: string[]) => void;
}) {
  const [chosen, setChosen] = useState<Set<string>>(
    () => new Set(proposals.filter((p) => p.confidence >= 0.8).map((p) => p.id)),
  );
  const patch = usePatchModel();
  const picked = proposals.filter((p) => chosen.has(p.id));
  if (proposals.length === 0) return <p className="text-sm text-zinc-600 dark:text-zinc-400">No relationships to suggest.</p>;
  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {proposals.map((proposal) => (
          <li key={proposal.id} className="rounded-md border border-zinc-200 p-3 text-sm dark:border-zinc-800">
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                className="mt-1"
                checked={chosen.has(proposal.id)}
                onChange={(e) => {
                  const next = new Set(chosen);
                  if (e.target.checked) next.add(proposal.id);
                  else next.delete(proposal.id);
                  setChosen(next);
                }}
              />
              <span className="min-w-0 flex-1">
                <span className="font-medium">{describeRelationship(proposal.relationship)}</span>{" "}
                <Badge tone={proposal.confidence >= 0.8 ? "good" : "neutral"}>{Math.round(proposal.confidence * 100)}%</Badge>
                {!proposal.relationship.active && <span className="ml-1 text-xs text-zinc-500">inactive</span>}
                <ul className="mt-1 list-disc pl-5 text-xs text-zinc-600 dark:text-zinc-400">
                  {proposal.evidence.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
                {proposal.inactiveBecause && <span className="mt-1 block text-xs text-zinc-500">{proposal.inactiveBecause}</span>}
              </span>
            </label>
          </li>
        ))}
      </ul>
      <SaveError error={patch.error} />
      <Button
        variant="primary"
        disabled={picked.length === 0}
        busy={patch.isPending}
        onClick={() =>
          patch.mutate(
            {
              ops: picked.map((p) => ({ op: "add" as const, path: pointer("relationships", p.id), value: p.relationship })),
              baseVersion: model.version,
              message: picked.length === 1 ? `Relationship ${picked[0]!.id}` : `${picked.length} suggested relationships`,
            },
            { onSuccess: () => onAdded(picked.map((p) => p.id)) },
          )
        }
      >
        {picked.length === 1 ? "Add 1 relationship" : `Add ${picked.length} relationships`}
      </Button>
    </div>
  );
}

export function SuggestionsPanel({ model, onClose }: { model: WorkspaceModel; onClose: () => void }) {
  const suggestions = useSuggestions();
  const { mutate } = suggestions;
  const [added, setAdded] = useState<string[] | null>(null);
  useEffect(() => mutate(undefined), [mutate]);
  return (
    <SidePanel title="Suggested relationships" onClose={onClose}>
      {suggestions.isPending && <p className="text-sm text-zinc-500">Reading the keys of every table.</p>}
      <ErrorNote error={suggestions.error} />
      {added && (
        <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
          Added {added.join(", ")}.
        </p>
      )}
      {suggestions.data && !added && (
        <>
          <ProposalList model={model} proposals={suggestions.data.proposals} onAdded={setAdded} />
          <p className="text-xs text-zinc-500">
            {suggestions.data.pairsChecked} pairs of fields checked. {suggestions.data.note}
          </p>
        </>
      )}
      {added && (
        <Button
          onClick={() => {
            setAdded(null);
            mutate(undefined);
          }}
        >
          Suggest again
        </Button>
      )}
    </SidePanel>
  );
}

export function DateTablePanel({ model, onClose }: { model: WorkspaceModel; onClose: () => void }) {
  const generate = useGenerateDateTable();
  const taken = new Set(Object.keys(model.document.tables));
  const [name, setName] = useState(taken.has("dates") ? "calendar" : "dates");
  const [label, setLabel] = useState("Dates");
  const [start, setStart] = useState("");
  const [added, setAdded] = useState<string[] | null>(null);
  const made = generate.data;
  return (
    <SidePanel title="Generate a date table" onClose={onClose}>
      {!made ? (
        <>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            A table with one row per day, from the start of the financial year of the earliest date in your tables to the
            end of the next financial year, with year, quarter, month, week and financial-year fields. It is refreshed
            every day and marked as the model&apos;s date table.
          </p>
          <Field label="Name" htmlFor="date-name" hint="The dataset's and the table's name.">
            <Input id="date-name" maxLength={63} value={name} onChange={(e) => setName(e.target.value.trim())} />
          </Field>
          <Field label="Label" htmlFor="date-label">
            <Input id="date-label" maxLength={200} value={label} onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Field label="First date (optional)" htmlFor="date-start" hint="Left empty, it comes from your data.">
            <Input id="date-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <SaveError error={generate.error} />
          <Button
            variant="primary"
            busy={generate.isPending}
            disabled={!name}
            onClick={() => generate.mutate({ name, label: label || undefined, start: start || undefined })}
          >
            Generate
          </Button>
        </>
      ) : (
        <>
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            Made {made.table}: {made.rows?.toLocaleString("en-AU") ?? "no"} days from {made.start} to {made.end}.
          </p>
          <p className="text-xs text-zinc-500">{made.note}</p>
          {added ? (
            <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
              Added {added.join(", ")}.
            </p>
          ) : (
            <>
              <h3 className="text-sm font-semibold">Relate it to your dates</h3>
              <ProposalList model={model} proposals={made.proposals} onAdded={setAdded} />
            </>
          )}
        </>
      )}
    </SidePanel>
  );
}
