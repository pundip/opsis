// What a refresh changed, column by column (values that could not be stored in
// their column's type become NULL and are counted), and the column types a
// person chooses by hand on the dataset page, with the types Opsis suggests.
import { useState } from "react";
import {
  type ColumnOverride,
  type DataQuality,
  type Dataset,
  type OverrideType,
  type TypeSuggestion,
  useColumnTypes,
} from "../lib/data";
import { formatNumber } from "../lib/format";
import { Badge, Button, Card, ErrorNote, Input, ROW, Select, TD, TH } from "./ui";

export const TYPE_LABELS: Record<OverrideType, string> = {
  text: "Text",
  integer: "Whole number",
  decimal: "Decimal number",
  date: "Date",
  datetime: "Date and time",
  boolean: "True or false",
};

const INVALID_DATES: Record<DataQuality["invalidDates"], string> = {
  null: "stored as empty (NULL)",
  text: "kept, because the connection reads date columns as text",
  fail: "stop the refresh",
};

const values = (n: number) => `${formatNumber(n)} value${n === 1 ? "" : "s"}`;

function Samples({ samples, hidden }: { samples: string[]; hidden?: boolean }) {
  if (hidden) {
    return <span className="text-xs text-zinc-500">Not shown: row-level security applies to you.</span>;
  }
  return (
    <>
      {samples.map((sample) => (
        <code
          key={sample}
          className="mr-1 mb-1 inline-block rounded bg-zinc-100 px-1.5 py-0.5 text-xs whitespace-pre dark:bg-zinc-800"
        >
          {sample}
        </code>
      ))}
    </>
  );
}

/** The values one refresh stored as NULL, column by column. */
export function QualityTable({ report }: { report: Partial<DataQuality> }) {
  const emptied = report.emptied ?? [];
  if (!emptied.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full" aria-label="Values stored as NULL">
        <thead>
          <tr>
            <th className={TH}>Column</th>
            <th className={`${TH} text-right`}>Set to NULL</th>
            <th className={TH}>Why</th>
            <th className={TH}>Sample values in the source</th>
          </tr>
        </thead>
        <tbody>
          {emptied.map((entry) => (
            <tr key={entry.column} className={ROW}>
              <td className={TD}>
                <span className="font-medium">{entry.column}</span>
                <span className="block text-xs text-zinc-500">
                  {entry.sourceType ? `${entry.sourceType} in the source, ` : ""}stored as {entry.type}
                </span>
              </td>
              <td className={`${TD} text-right tabular-nums`}>{formatNumber(entry.count)}</td>
              <td className={TD}>
                {entry.reasons.map((reason) => (
                  <span key={reason.reason} className="block">
                    {reason.reason}
                    {entry.reasons.length > 1 && ` (${formatNumber(reason.count)})`}
                  </span>
                ))}
              </td>
              <td className={TD}>
                <Samples samples={entry.samples} hidden={entry.samplesHidden} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The dataset page's account of its last refresh. Nothing when there is nothing to say. */
export function DataQualityCard({ report }: { report: DataQuality | null | undefined }) {
  if (!report) return null;
  const textDates = report.textDates ?? [];
  const suggested = report.suggestions ?? [];
  if (!report.emptiedValues && !textDates.length && !suggested.length) return null;
  return (
    <section aria-labelledby="data-quality" className="mb-5">
      <h2 id="data-quality" className="mb-2 text-base font-semibold">
        Data quality
      </h2>
      <Card>
        {report.emptiedValues > 0 && (
          <p className="px-3 pt-3 text-sm">
            The last refresh stored {values(report.emptiedValues)} as empty (NULL), because they could not be stored in
            their column's type. The rest of each row was kept.
          </p>
        )}
        <QualityTable report={report} />
        {suggested.length > 0 && (
          <p className="border-t border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
            {suggested.length === 1 ? "One text column looks" : `${suggested.length} text columns look`} like another
            type: {suggested.map((s) => `${s.column} (${TYPE_LABELS[s.type].toLowerCase()})`).join(", ")}. Nothing
            changes until you choose it under Column types.
          </p>
        )}
        <p className="border-t border-zinc-200 px-3 py-2 text-xs text-zinc-500 dark:border-zinc-800">
          Invalid dates such as 0000-00-00 are {INVALID_DATES[report.invalidDates]}, as the connection is set.
          {textDates.length > 0 && ` Read as text: ${textDates.join(", ")}.`}
        </p>
      </Card>
    </section>
  );
}

/** One refresh job's count of NULLs, opening to the columns, for job lists. */
export function JobQuality({ report }: { report: Partial<DataQuality> | undefined }) {
  if (!report?.emptiedValues) return null;
  return (
    <details className="mt-0.5 text-xs text-amber-800 dark:text-amber-300">
      <summary className="cursor-pointer">{formatNumber(report.emptiedValues)} set to NULL</summary>
      <ul className="mt-1 text-left text-zinc-700 dark:text-zinc-300">
        {(report.emptied ?? []).map((entry) => (
          <li key={entry.column}>
            {entry.column}: {formatNumber(entry.count)} ({entry.reasons.map((r) => r.reason).join(", ")})
            {!entry.samplesHidden && entry.samples.length > 0 && `, e.g. ${entry.samples.slice(0, 2).join(", ")}`}
          </li>
        ))}
      </ul>
    </details>
  );
}

type Draft = Record<string, ColumnOverride>;

function cleaned(draft: Draft): Draft {
  const result: Draft = {};
  for (const [name, chosen] of Object.entries(draft)) {
    const dated = chosen.type === "date" || chosen.type === "datetime";
    const format = dated ? (chosen.format ?? "").trim() : "";
    result[name] = format ? { type: chosen.type, format } : { type: chosen.type };
  }
  return result;
}

function same(a: Draft, b: Draft): boolean {
  const left = cleaned(a);
  const right = cleaned(b);
  const entries = Object.entries(left);
  return (
    entries.length === Object.keys(right).length &&
    entries.every(([name, chosen]) => {
      const other = right[name];
      return other !== undefined && chosen.type === other.type && (chosen.format ?? "") === (other.format ?? "");
    })
  );
}

function Suggestion({ suggestion, onUse }: { suggestion: TypeSuggestion; onUse: () => void }) {
  const lost = (suggestion.invalidDates ?? 0) + (suggestion.emptyWords ?? 0);
  return (
    <span className="mt-1 block text-xs text-sky-800 dark:text-sky-300">
      Looks like {TYPE_LABELS[suggestion.type].toLowerCase()}
      {suggestion.format && ` written ${suggestion.format}`} ({formatNumber(suggestion.checked)} values checked
      {lost > 0 && `; ${values(lost)} such as 0000-00-00 or n/a would be NULL`}).
      {suggestion.note && ` ${suggestion.note}`}{" "}
      <button
        type="button"
        className="text-brand underline"
        aria-label={`Use the suggested type for ${suggestion.column}`}
        onClick={onUse}
      >
        Use this type
      </button>
    </span>
  );
}

/** Choose column types by hand. Saving rebuilds the dataset with them. */
export function ColumnTypes({ dataset }: { dataset: Dataset }) {
  const save = useColumnTypes(dataset.id);
  const saved: Draft = dataset.columnOverrides ?? {};
  const [draft, setDraft] = useState<Draft>(saved);
  const report = dataset.snapshot?.report ?? null;
  const columns = dataset.snapshot?.columns ?? [];
  if (!columns.length) return null;
  const suggestions = new Map((report?.suggestions ?? []).map((s) => [s.column, s]));
  const sourceTypes = report?.sourceTypes ?? {};
  const changed = !same(draft, saved);

  const choose = (name: string, chosen: ColumnOverride | null) =>
    setDraft((current) => {
      const next = { ...current };
      if (chosen) next[name] = chosen;
      else delete next[name];
      return next;
    });

  return (
    <section aria-labelledby="column-types" className="mb-5">
      <h2 id="column-types" className="mb-2 text-base font-semibold">
        Column types
      </h2>
      <Card>
        <p className="px-3 pt-3 text-sm text-zinc-600 dark:text-zinc-400">
          Each column is stored in the type its source gives it. Choose another type when that is wrong, for example a
          text column that holds dates. A value that does not fit the chosen type is stored as NULL and reported. Your
          choices are kept when the source's columns are read again.
        </p>
        <div className="max-h-[32rem] overflow-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Column</th>
                <th className={TH}>Type</th>
                <th className={TH}>Date format</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((column) => {
                const chosen = draft[column.name];
                const suggestion = suggestions.get(column.name);
                const dated = chosen?.type === "date" || chosen?.type === "datetime";
                const offered =
                  suggestion &&
                  !(chosen?.type === suggestion.type && (chosen.format ?? null) === (suggestion.format ?? null));
                return (
                  <tr key={column.name} className={ROW}>
                    <td className={TD}>
                      <span className="font-medium">{column.name}</span>{" "}
                      {saved[column.name] && <Badge>Set by hand</Badge>}
                      <span className="block text-xs text-zinc-500">
                        {sourceTypes[column.name] ? `${sourceTypes[column.name]} in the source, ` : ""}stored as{" "}
                        {column.type}
                      </span>
                      {offered && suggestion && (
                        <Suggestion
                          suggestion={suggestion}
                          onUse={() => choose(column.name, { type: suggestion.type, format: suggestion.format })}
                        />
                      )}
                    </td>
                    <td className={TD}>
                      <Select
                        aria-label={`Type of ${column.name}`}
                        value={chosen?.type ?? ""}
                        onChange={(event) => {
                          const type = event.target.value as OverrideType | "";
                          choose(column.name, type ? { type, format: chosen?.format ?? null } : null);
                        }}
                      >
                        <option value="">As the source gives it</option>
                        {(Object.keys(TYPE_LABELS) as OverrideType[]).map((type) => (
                          <option key={type} value={type}>
                            {TYPE_LABELS[type]}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className={TD}>
                      {dated && chosen && (
                        <Input
                          aria-label={`Date format of ${column.name}`}
                          placeholder="ISO 8601, or for example %d/%m/%Y"
                          value={chosen.format ?? ""}
                          onChange={(event) => choose(column.name, { ...chosen, format: event.target.value })}
                        />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-zinc-200 px-3 py-2 dark:border-zinc-800">
          <Button
            variant="primary"
            disabled={!changed}
            busy={save.isPending}
            onClick={() =>
              save.mutate(cleaned(draft), { onSuccess: (done) => setDraft(done.columnOverrides ?? {}) })
            }
          >
            Save and rebuild
          </Button>
          {changed && (
            <Button variant="ghost" onClick={() => setDraft(saved)}>
              Undo changes
            </Button>
          )}
          <span className="text-xs text-zinc-500">
            Formats use % codes: %d day, %m month, %Y year, %H:%M hours and minutes. Leave it empty for 2026-12-31.
          </span>
        </div>
        <div className="px-3 pb-2">
          <ErrorNote error={save.error} />
          {save.data?.job && !changed && (
            <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
              Saved. The dataset is being rebuilt with these types.
            </p>
          )}
        </div>
      </Card>
    </section>
  );
}
