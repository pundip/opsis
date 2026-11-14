import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../lib/api";
import { periodLabel } from "../lib/calendar";
import type { AppConfig, Me } from "../lib/config";
import { formatCell, formatMeasure, formatNumber, timeAgo } from "../lib/format";
import { url } from "../lib/runtime";
import {
  type Field as ModelField,
  GRAIN_LABELS,
  type Grain,
  type ModelDetail as Model,
  type PreviewAs,
  type Problem,
  type QueryResult,
  RELATIVE_RANGES,
  type SemanticQuery,
  useModel,
  useModelVersions,
  useRestoreModelVersion,
  useRunQuery,
  useSaveModel,
  useStarterModel,
  useValidateModel,
} from "../lib/semantic";
import { Badge, Button, Card, ErrorNote, Field, Input, PageHeader, Select } from "../components/ui";

const OPERATORS: [string, string][] = [
  ["eq", "is"],
  ["neq", "is not"],
  ["in", "is one of (comma separated)"],
  ["contains", "contains"],
  ["gt", "greater than"],
  ["gte", "at least"],
  ["lt", "less than"],
  ["lte", "at most"],
  ["is_null", "is empty"],
  ["is_not_null", "is not empty"],
];

function ProblemList({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) return null;
  return (
    <ul role="alert" className="space-y-1 rounded-md border border-red-200 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950">
      {problems.map((problem) => (
        <li key={problem.path + problem.message} className="text-red-800 dark:text-red-300">
          <code className="font-mono text-xs">{problem.path}</code> {problem.message}
        </li>
      ))}
    </ul>
  );
}

function Versions({ model, canEdit }: { model: Model; canEdit: boolean }) {
  const name = model.dataset.name;
  const versions = useModelVersions(name);
  const restore = useRestoreModelVersion(name);
  const details = restore.error instanceof ApiError ? (restore.error.details as Problem[]) : [];
  return (
    <Card className="p-4">
      <h2 className="mb-3 text-base font-semibold">Version history</h2>
      <ProblemList problems={details} />
      {!details.length && <ErrorNote error={versions.error ?? restore.error} />}
      <ul className="max-h-72 divide-y divide-zinc-200 overflow-auto dark:divide-zinc-800">
        {versions.data?.map((version) => (
          <li key={version.version} className="flex flex-wrap items-center gap-2 py-2 text-sm">
            <span className="font-medium">Version {version.version}</span>
            {version.current && <Badge tone="good">Current</Badge>}
            <span className="text-zinc-600 dark:text-zinc-400">
              {version.authorName}, {timeAgo(version.createdAt)}
              {version.message && `: ${version.message}`}
            </span>
            {canEdit && !version.current && (
              <Button
                variant="ghost"
                className="ml-auto"
                busy={restore.isPending && restore.variables === version.version}
                onClick={() => {
                  if (window.confirm(`Restore version ${version.version}? It becomes a new version.`)) {
                    restore.mutate(version.version);
                  }
                }}
              >
                Restore
              </Button>
            )}
          </li>
        ))}
        {versions.data?.length === 0 && (
          <li className="py-2 text-sm text-zinc-500">No saved versions yet.</li>
        )}
      </ul>
    </Card>
  );
}

function Editor({ model, canEdit }: { model: Model; canEdit: boolean }) {
  const name = model.dataset.name;
  const [text, setText] = useState(model.yaml);
  const [message, setMessage] = useState("");
  const save = useSaveModel(name);
  const validate = useValidateModel(name);
  const starter = useStarterModel(name);
  const dirty = text !== model.yaml;
  const failure = save.error ?? validate.error ?? starter.error;
  const problems =
    save.error instanceof ApiError && save.error.details.length
      ? (save.error.details as Problem[])
      : (validate.data?.problems ?? []);

  useEffect(() => setText(model.yaml), [model.yaml]);

  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="mr-auto text-base font-semibold">Model definition (YAML)</h2>
        <a
          href={url(`/api/models/${name}/export`)}
          className="text-sm text-brand underline"
          download={`${name}.yml`}
        >
          Download {name}.yml
        </a>
      </div>
      <label htmlFor="model-yaml" className="sr-only">
        Model definition in YAML
      </label>
      <textarea
        id="model-yaml"
        value={text}
        readOnly={!canEdit}
        spellCheck={false}
        onChange={(event) => {
          setText(event.target.value);
          save.reset();
          validate.reset();
        }}
        className="h-[28rem] w-full rounded-md border border-zinc-300 bg-zinc-50 p-3 font-mono text-xs leading-5 dark:border-zinc-700 dark:bg-zinc-950"
      />
      <div className="mt-3 space-y-3">
        <ProblemList problems={problems} />
        {!problems.length && <ErrorNote error={failure} />}
        {validate.data?.valid && !dirty && (
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            The model is valid.
          </p>
        )}
        {validate.data?.valid && dirty && (
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            The model is valid. Save it to use it in queries.
          </p>
        )}
        {save.isSuccess && !dirty && (
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            Saved.
          </p>
        )}
        {canEdit && dirty && (
          <Input
            aria-label="Note for the version history (optional)"
            placeholder="Note for the version history (optional)"
            maxLength={500}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
          />
        )}
        {canEdit && (
          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              busy={save.isPending}
              disabled={!dirty}
              onClick={() =>
                save.mutate(
                  { yaml: text, baseVersion: model.version, message: message || undefined },
                  {
                    onSuccess: (saved) => {
                      setText(saved.yaml);
                      setMessage("");
                    },
                  },
                )
              }
            >
              Save model
            </Button>
            <Button busy={validate.isPending} onClick={() => validate.mutate(text)}>
              Check
            </Button>
            <Button variant="ghost" disabled={!dirty} onClick={() => setText(model.yaml)}>
              Discard changes
            </Button>
            <Button
              variant="ghost"
              busy={starter.isPending}
              title="Replace the text with a model generated from the dataset's columns. Nothing is saved until you press Save."
              onClick={() => starter.mutate(undefined, { onSuccess: (data) => setText(data.yaml) })}
            >
              Regenerate from columns
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

function CheckList(props: {
  legend: string;
  fields: ModelField[];
  selected: string[];
  onChange: (next: string[]) => void;
  baseDataset: string;
}) {
  if (props.fields.length === 0) return null;
  const toggle = (ref: string) =>
    props.onChange(
      props.selected.includes(ref)
        ? props.selected.filter((item) => item !== ref)
        : [...props.selected, ref],
    );
  return (
    <fieldset>
      <legend className="mb-1 text-sm font-medium">{props.legend}</legend>
      <div className="max-h-44 space-y-0.5 overflow-auto rounded-md border border-zinc-200 p-2 dark:border-zinc-800">
        {props.fields.map((field) => (
          <label key={field.ref} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={props.selected.includes(field.ref)}
              onChange={() => toggle(field.ref)}
            />
            <span>{field.label}</span>
            {field.dataset !== props.baseDataset && (
              <span className="text-xs text-zinc-500">from {field.dataset}</span>
            )}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function Results({ result, config }: { result: QueryResult; config: AppConfig }) {
  const display = (value: unknown, column: QueryResult["columns"][number]) => {
    if (value === null) return <span className="text-zinc-400 italic">null</span>;
    if (column.role === "time" && column.grain) {
      return periodLabel(String(value), column.grain, config.calendar);
    }
    if (column.role === "measure") return formatMeasure(value, column.format);
    return formatCell(value, column.type);
  };
  return (
    <div>
      <p className="mb-2 text-xs text-zinc-500" role="status">
        {formatNumber(result.rowCount)} row{result.rowCount === 1 ? "" : "s"}
        {result.truncated && " (more rows exist; raise the limit to see them)"},{" "}
        {result.cacheHit ? "from cache" : `${Math.round(result.elapsedMs)} ms`}{" "}
        {result.rlsBypassed && (
          <span title="This dataset has row-level security rules that do not apply to the role this query ran as.">
            <Badge tone="bad">RLS bypassed</Badge>
          </span>
        )}
      </p>
      <div className="max-h-96 overflow-auto rounded-md border border-zinc-200 dark:border-zinc-800">
        <table className="w-full border-separate border-spacing-0 text-sm">
          <thead>
            <tr>
              {result.columns.map((column) => (
                <th
                  key={column.name}
                  scope="col"
                  className={`sticky top-0 border-b border-zinc-200 bg-zinc-50 px-3 py-2 whitespace-nowrap dark:border-zinc-800 dark:bg-zinc-900 ${column.role === "measure" ? "text-right" : "text-left"}`}
                >
                  <span className="font-medium">{column.label ?? column.name}</span>
                  <span className="block font-mono text-xs font-normal text-zinc-500">
                    {column.name}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, index) => (
              <tr key={index} className="odd:bg-zinc-50/60 dark:odd:bg-zinc-800/30">
                {row.map((cell, cellIndex) => {
                  const column = result.columns[cellIndex]!;
                  return (
                    <td
                      key={cellIndex}
                      className={`border-b border-zinc-100 px-3 py-1.5 whitespace-nowrap dark:border-zinc-800 ${column.role === "measure" ? "text-right tabular-nums" : ""}`}
                    >
                      {display(cell, column)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.sql && (
        <details className="mt-3">
          <summary className="cursor-pointer text-sm text-zinc-600 dark:text-zinc-400">
            Generated SQL
          </summary>
          <pre className="mt-2 overflow-auto rounded-md bg-zinc-100 p-3 font-mono text-xs whitespace-pre-wrap dark:bg-zinc-950">
            {result.sql}
          </pre>
        </details>
      )}
    </div>
  );
}

function QueryBuilder({ model, config, me }: { model: Model; config: AppConfig; me: Me }) {
  const base = model.dataset.name;
  const dimensions = model.fields.filter((f) => f.kind === "dimension");
  const measures = model.fields.filter((f) => f.kind === "measure");
  const segments = model.fields.filter((f) => f.kind === "segment");
  const timeFields = dimensions.filter((f) => f.type === "time");
  const isAdmin = me.permissions.includes("settings:manage");

  const [picked, setPicked] = useState<string[]>([]);
  const [values, setValues] = useState<string[]>(measures[0] ? [measures[0].ref] : []);
  const [applied, setApplied] = useState<string[]>([]);
  const [timeField, setTimeField] = useState("");
  const [grain, setGrain] = useState<Grain | "">("month");
  const [range, setRange] = useState("");
  const [filterField, setFilterField] = useState("");
  const [filterOp, setFilterOp] = useState("eq");
  const [filterValue, setFilterValue] = useState("");
  const [limit, setLimit] = useState(100);
  const [asRole, setAsRole] = useState<"" | PreviewAs["role"]>("");
  const [attributes, setAttributes] = useState('{"regions": ["VIC"]}');
  const [formError, setFormError] = useState("");
  const run = useRunQuery();
  const grains = timeFields.find((f) => f.ref === timeField)?.grains ?? [];

  function build(): { query: SemanticQuery; previewAs?: PreviewAs } | null {
    setFormError("");
    const query: SemanticQuery = { dataset: base, limit };
    if (picked.length) query.dimensions = picked.filter((ref) => ref !== timeField || !grain);
    if (values.length) query.measures = values;
    if (applied.length) query.segments = applied;
    if (timeField) {
      query.timeDimension = { field: timeField };
      if (grain && grains.includes(grain)) query.timeDimension.grain = grain;
      if (range) query.timeDimension.range = { relative: range };
      if (!query.timeDimension.grain && !query.timeDimension.range) delete query.timeDimension;
    }
    if (filterField) {
      const field = model.fields.find((f) => f.ref === filterField);
      const numeric = field?.type === "number";
      const convert = (text: string) => (numeric && text.trim() !== "" ? Number(text) : text.trim());
      const noValue = filterOp === "is_null" || filterOp === "is_not_null";
      const raw = filterOp === "in" ? filterValue.split(",") : [filterValue];
      query.filters = [
        { field: filterField, op: filterOp, ...(noValue ? {} : { values: raw.map(convert) }) },
      ];
    }
    if (!query.dimensions?.length) delete query.dimensions;
    if (!query.dimensions && !query.measures && !query.timeDimension) {
      setFormError("Choose at least one dimension or measure.");
      return null;
    }
    if (!asRole) return { query };
    try {
      const parsed: unknown = JSON.parse(attributes || "{}");
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
      return { query, previewAs: { role: asRole, attributes: parsed as Record<string, unknown> } };
    } catch {
      setFormError('User attributes must be a JSON object, for example {"regions": ["VIC"]}.');
      return null;
    }
  }

  const details = run.error instanceof ApiError ? (run.error.details as Problem[]) : [];

  return (
    <Card className="p-4">
      <h2 className="mb-3 text-base font-semibold">Try a query</h2>
      <form
        className="grid gap-4 lg:grid-cols-3"
        onSubmit={(event) => {
          event.preventDefault();
          const input = build();
          if (input) run.mutate(input);
        }}
      >
        <div className="space-y-4">
          <CheckList legend="Measures" fields={measures} selected={values} onChange={setValues} baseDataset={base} />
          <CheckList legend="Segments" fields={segments} selected={applied} onChange={setApplied} baseDataset={base} />
        </div>
        <CheckList legend="Group by" fields={dimensions} selected={picked} onChange={setPicked} baseDataset={base} />
        <div className="space-y-3">
          <Field label="Date field" htmlFor="q-time">
            <Select id="q-time" value={timeField} onChange={(e) => setTimeField(e.target.value)}>
              <option value="">None</option>
              {timeFields.map((field) => (
                <option key={field.ref} value={field.ref}>
                  {field.label}
                </option>
              ))}
            </Select>
          </Field>
          {timeField && (
            <div className="grid grid-cols-2 gap-2">
              <Field label="Group dates by" htmlFor="q-grain">
                <Select id="q-grain" value={grain} onChange={(e) => setGrain(e.target.value as Grain | "")}>
                  <option value="">Do not group</option>
                  {grains.map((g) => (
                    <option key={g} value={g}>
                      {GRAIN_LABELS[g]}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Date range" htmlFor="q-range">
                <Select id="q-range" value={range} onChange={(e) => setRange(e.target.value)}>
                  {RELATIVE_RANGES.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
          )}
          <Field label="Filter" htmlFor="q-filter-field">
            <Select id="q-filter-field" value={filterField} onChange={(e) => setFilterField(e.target.value)}>
              <option value="">No filter</option>
              {[...dimensions, ...measures].map((field) => (
                <option key={field.ref} value={field.ref}>
                  {field.label}
                </option>
              ))}
            </Select>
          </Field>
          {filterField && (
            <div className="grid grid-cols-2 gap-2">
              <Select aria-label="Filter condition" value={filterOp} onChange={(e) => setFilterOp(e.target.value)}>
                {OPERATORS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
              {filterOp !== "is_null" && filterOp !== "is_not_null" && (
                <Input aria-label="Filter value" value={filterValue} onChange={(e) => setFilterValue(e.target.value)} />
              )}
            </div>
          )}
          <Field label="Row limit" htmlFor="q-limit">
            <Input
              id="q-limit"
              type="number"
              min={1}
              max={10000}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value) || 100)}
            />
          </Field>
          {isAdmin && (
            <Field
              label="Run as"
              htmlFor="q-role"
              hint="Check row-level security by running the query as another role."
            >
              <Select id="q-role" value={asRole} onChange={(e) => setAsRole(e.target.value as typeof asRole)}>
                <option value="">Myself ({me.role})</option>
                <option value="viewer">A viewer</option>
                <option value="editor">An editor</option>
              </Select>
            </Field>
          )}
          {asRole && (
            <Field label="That user's attributes (JSON)" htmlFor="q-attributes">
              <textarea
                id="q-attributes"
                value={attributes}
                spellCheck={false}
                onChange={(e) => setAttributes(e.target.value)}
                className="h-16 w-full rounded-md border border-zinc-300 bg-white p-2 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-950"
              />
            </Field>
          )}
          <Button type="submit" variant="primary" busy={run.isPending}>
            Run query
          </Button>
        </div>
      </form>
      <div className="mt-4 space-y-3">
        {formError && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {formError}
          </p>
        )}
        <ProblemList problems={details} />
        {!details.length && <ErrorNote error={run.error} />}
        {run.data && <Results result={run.data} config={config} />}
      </div>
    </Card>
  );
}

export function ModelDetail({ config, me }: { config: AppConfig; me: Me }) {
  const { dataset = "" } = useParams();
  const model = useModel(dataset);

  if (model.isPending) return <p className="text-sm text-zinc-500">Loading...</p>;
  if (model.isError) {
    return (
      <div className="mx-auto max-w-5xl">
        <ErrorNote error={model.error} />
        <Link to="/models" className="mt-3 inline-block text-sm text-brand underline">
          Back to semantic models
        </Link>
      </div>
    );
  }
  const data = model.data;
  return (
    <div className="mx-auto max-w-6xl">
      <p className="mb-2 text-sm">
        <Link to="/models" className="text-brand underline">
          Semantic models
        </Link>
        <span className="text-zinc-500"> / {data.dataset.name}</span>
      </p>
      <PageHeader
        title={data.dataset.label}
        description={
          <>
            Model of the <Link to={`/datasets/${data.dataset.id}`} className="text-brand underline">{data.dataset.name}</Link>{" "}
            dataset. {data.isStarter ? <Badge>Generated</Badge> : <Badge tone="good">Edited</Badge>}
          </>
        }
      />
      {!data.dataset.hasData ? (
        <Card className="p-4 text-sm text-zinc-600 dark:text-zinc-400">
          This dataset has no data yet. Refresh it first; a starter model is generated from its
          columns.
        </Card>
      ) : (
        <div className="space-y-5">
          <QueryBuilder key={data.updatedAt ?? "new"} model={data} config={config} me={me} />
          <Editor model={data} canEdit={me.permissions.includes("models:edit")} />
          <Versions model={data} canEdit={me.permissions.includes("models:edit")} />
        </div>
      )}
    </div>
  );
}
