// The editor's side panels: the selected visual, filters, dashboard settings and history.
import { useState } from "react";
import { ApiError } from "../../lib/api";
import {
  type Binding,
  type FilterControl,
  type FilterType,
  type Item,
  type Spec,
  bindingField,
  bindingAxis,
  bindingGrain,
  defaultConfig,
  newId,
  useDashboardVersion,
  useDashboardVersions,
  useRestoreDashboard,
} from "../../lib/dashboard";
import { countChanges, diffLines, withContext } from "../../lib/diff";
import { timeAgo } from "../../lib/format";
import { type Field as ModelField, GRAIN_LABELS, type Grain, RELATIVE_RANGES } from "../../lib/semantic";
import { manifests, visualList } from "../../visuals/registry";
import { ConfigForm } from "../ConfigForm";
import { Badge, Button, ErrorNote, Field, Input, Select } from "../ui";

const SECTION = "border-t border-zinc-200 pt-3 dark:border-zinc-800";
const short = (ref: string) => ref.split(".")[1]?.replace(/_/g, " ") ?? ref;

function fieldLabel(fields: ModelField[], ref: string): string {
  return fields.find((f) => f.ref === ref)?.label ?? short(ref);
}

// ---- the selected visual --------------------------------------------------------------

interface ItemPanelProps {
  item: Item;
  datasets: string[];
  fields: ModelField[];
  onChange: (item: Item) => void;
  onRemove: () => void;
}

export function ItemPanel({ item, datasets, fields, onChange, onRemove }: ItemPanelProps) {
  // Drag and drop: a field from the list (from undefined) or from another role.
  const [dragging, setDragging] = useState<{ ref: string; from?: string } | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const manifest = manifests[item.visual];
  if (!manifest) return <p className="text-sm text-red-700">There is no visual called "{item.visual}".</p>;
  const roles = Object.entries(manifest.roles);
  type Rule = (typeof roles)[number][1];
  const bindings = item.bindings ?? {};
  const boundRefs = Object.values(bindings).flat().map(bindingField);
  const setAll = (all: Record<string, Binding[]>) => {
    const still = new Set(Object.values(all).flat().map(bindingField));
    onChange({ ...item, bindings: all, sort: item.sort?.filter((s) => still.has(s.field)) });
  };
  const setBindings = (role: string, next: Binding[]) => setAll({ ...bindings, [role]: next });
  const hasGrain = Object.values(bindings).flat().some((b) => bindingGrain(b));
  const fits = (rule: Rule, field: ModelField) =>
    (field.kind === "dimension" || (field.kind === "measure" && field.dataset === item.dataset)) &&
    (rule.kind === "any" || rule.kind === field.kind) &&
    (!rule.types || field.kind !== "dimension" || rule.types.includes(field.type));
  // A date on a chart starts grouped by month; a table shows each value.
  const firstEntry = (field: ModelField): Binding => {
    const grain =
      field.type === "time" && item.visual !== "table" && !hasGrain
        ? field.grains?.includes("month")
          ? "month"
          : field.grains?.[0]
        : undefined;
    return grain ? { field: field.ref, grain } : field.ref;
  };
  /** Add a field to a role, or move it there from the role it was dragged from. */
  const place = (role: string, rule: Rule, field: ModelField, from?: string) => {
    const target = bindings[role] ?? [];
    if (from === role || target.length >= rule.max || !fits(rule, field)) return;
    if (!from) {
      setBindings(role, [...target, firstEntry(field)]);
      return;
    }
    const moved = (bindings[from] ?? []).find((e) => bindingField(e) === field.ref) ?? field.ref;
    // A right-hand axis only means something in a role that has axes.
    const entry: Binding =
      typeof moved === "string" || rule.axis || !moved.axis ? moved : moved.grain ? { field: moved.field, grain: moved.grain } : moved.field;
    setAll({ ...bindings, [from]: (bindings[from] ?? []).filter((e) => bindingField(e) !== field.ref), [role]: [...target, entry] });
  };
  const dragged = dragging ? fields.find((f) => f.ref === dragging.ref) : undefined;
  const endDrag = () => {
    setDragging(null);
    setOver(null);
  };
  const palette = fields.filter(
    (f) => (f.kind === "dimension" || (f.kind === "measure" && f.dataset === item.dataset)) && !boundRefs.includes(f.ref),
  );

  return (
    <div className="space-y-3">
      <Field label="Visual" htmlFor="item-visual">
        <Select
          id="item-visual"
          value={item.visual}
          onChange={(event) => {
            const next = manifests[event.target.value]!;
            const kept = Object.fromEntries(Object.entries(bindings).filter(([role]) => role in next.roles));
            onChange({ ...item, visual: next.id, bindings: kept, config: defaultConfig(next), sort: undefined });
          }}
        >
          {visualList.map((visual) => (
            <option key={visual.id} value={visual.id}>
              {visual.name}
            </option>
          ))}
        </Select>
        <p className="mt-1 text-xs text-zinc-500">{manifest.description}</p>
      </Field>
      <Field label="Title" htmlFor="item-title" hint="Leave empty to use the measure names.">
        <Input id="item-title" value={item.title ?? ""} onChange={(e) => onChange({ ...item, title: e.target.value || undefined })} />
      </Field>

      {roles.length > 0 && (
        <div className={`${SECTION} space-y-3`}>
          <Field label="Dataset" htmlFor="item-dataset">
            <Select
              id="item-dataset"
              value={item.dataset ?? ""}
              onChange={(e) => onChange({ ...item, dataset: e.target.value || undefined, bindings: {}, sort: undefined, filters: undefined, segments: undefined })}
            >
              <option value="">Choose a dataset</option>
              {datasets.map((dataset) => (
                <option key={dataset} value={dataset}>
                  {dataset}
                </option>
              ))}
            </Select>
          </Field>
          {item.dataset && palette.length > 0 && (
            <div>
              <p id="fields-hint" className="mb-1 text-xs text-zinc-500">
                Drag a field into a role below, or choose it under Add a field.
              </p>
              <ul aria-label="Fields" aria-describedby="fields-hint" className="flex max-h-40 flex-wrap gap-1 overflow-auto">
                {palette.map((field) => (
                  <li
                    key={field.ref}
                    draggable
                    title={field.ref}
                    onDragStart={(event) => {
                      event.dataTransfer.setData("text/plain", field.ref);
                      event.dataTransfer.effectAllowed = "copy";
                      setDragging({ ref: field.ref });
                    }}
                    onDragEnd={endDrag}
                    className={`cursor-grab rounded border px-1.5 py-0.5 text-xs ${
                      field.kind === "measure"
                        ? "border-sky-300 bg-sky-50 dark:border-sky-800 dark:bg-sky-950/40"
                        : "border-zinc-300 bg-white dark:border-zinc-700 dark:bg-zinc-900"
                    }`}
                  >
                    {field.label}
                    {field.dataset !== item.dataset ? ` (${field.dataset})` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {item.dataset &&
            roles.map(([role, rule]) => {
              const entries = bindings[role] ?? [];
              const eligible = fields.filter((f) => !boundRefs.includes(f.ref) && fits(rule, f));
              const count = rule.min === rule.max ? `${rule.min}` : `${rule.min} to ${rule.max}`;
              const accepts = dragged !== undefined && dragging?.from !== role && entries.length < rule.max && fits(rule, dragged);
              return (
                <fieldset
                  key={role}
                  data-role={role}
                  className={`rounded-md ${over === role ? "outline outline-2 outline-brand" : accepts ? "outline-1 outline-dashed outline-zinc-400" : ""}`}
                  onDragOver={(event) => {
                    if (!accepts) return;
                    event.preventDefault();
                    event.dataTransfer.dropEffect = dragging?.from ? "move" : "copy";
                    if (over !== role) setOver(role);
                  }}
                  onDragLeave={(event) => {
                    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver((now) => (now === role ? null : now));
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    if (dragged && accepts) place(role, rule, dragged, dragging?.from);
                    endDrag();
                  }}
                >
                  <legend className="mb-1 text-sm font-medium">
                    {rule.label ?? role} <span className="font-normal text-zinc-500">({count})</span>
                  </legend>
                  <ul className="space-y-1">
                    {entries.map((entry, index) => {
                      const ref = bindingField(entry);
                      const field = fields.find((f) => f.ref === ref);
                      const replace = (next: Binding) => setBindings(role, entries.map((e, i) => (i === index ? next : e)));
                      return (
                        <li
                          key={ref}
                          draggable
                          title={`${ref}: drag it to another role to move it`}
                          onDragStart={(event) => {
                            event.dataTransfer.setData("text/plain", ref);
                            event.dataTransfer.effectAllowed = "move";
                            setDragging({ ref, from: role });
                          }}
                          onDragEnd={endDrag}
                          className="flex cursor-grab items-center gap-1 rounded-md bg-zinc-100 px-2 py-1 text-sm dark:bg-zinc-800"
                        >
                          <span className="mr-auto truncate">{fieldLabel(fields, ref)}</span>
                          {field?.type === "time" && item.visual !== "table" && (
                            <select
                              aria-label={`Group ${fieldLabel(fields, ref)} by`}
                              className="rounded border border-zinc-300 bg-transparent text-xs dark:border-zinc-600"
                              value={bindingGrain(entry) ?? ""}
                              onChange={(e) => replace(e.target.value ? { field: ref, grain: e.target.value as Grain } : ref)}
                            >
                              <option value="">Each value</option>
                              {(field.grains ?? []).map((grain) => (
                                <option key={grain} value={grain}>
                                  {GRAIN_LABELS[grain]}
                                </option>
                              ))}
                            </select>
                          )}
                          {rule.axis && (
                            <select
                              aria-label={`Value axis for ${fieldLabel(fields, ref)}`}
                              title="Which value axis this measure is drawn against. Use the right axis for a measure in a different unit, such as a count next to an amount."
                              className="rounded border border-zinc-300 bg-transparent text-xs dark:border-zinc-600"
                              value={bindingAxis(entry)}
                              onChange={(e) => replace(e.target.value === "right" ? { field: ref, axis: "right" } : ref)}
                            >
                              <option value="left">Left axis</option>
                              <option value="right">Right axis</option>
                            </select>
                          )}
                          <button
                            type="button"
                            aria-label={`Remove ${fieldLabel(fields, ref)}`}
                            className="px-1 text-zinc-500 hover:text-red-700"
                            onClick={() => setBindings(role, entries.filter((_, i) => i !== index))}
                          >
                            ×
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                  {entries.length < rule.max && (
                    <Select
                      aria-label={`Add a field to ${rule.label ?? role}`}
                      className="mt-1"
                      value=""
                      onChange={(event) => {
                        const field = fields.find((f) => f.ref === event.target.value);
                        if (field) place(role, rule, field);
                      }}
                    >
                      <option value="">Add a field...</option>
                      {eligible.map((f) => (
                        <option key={f.ref} value={f.ref}>
                          {f.label}
                          {f.dataset !== item.dataset ? ` (${f.dataset})` : ""}
                          {f.kind === "measure" ? " · measure" : ""}
                        </option>
                      ))}
                    </Select>
                  )}
                </fieldset>
              );
            })}
          {item.dataset && (
            <div className="grid grid-cols-2 gap-2">
              <Field label="Sort by" htmlFor="item-sort">
                <Select
                  id="item-sort"
                  value={item.sort?.[0] ? `${item.sort[0].field}|${item.sort[0].dir ?? "asc"}` : ""}
                  onChange={(e) => {
                    const [field, dir] = e.target.value.split("|");
                    onChange({ ...item, sort: field ? [{ field, dir: dir as "asc" | "desc" }] : undefined });
                  }}
                >
                  <option value="">Default order</option>
                  {boundRefs.flatMap((ref) => [
                    <option key={`${ref}|desc`} value={`${ref}|desc`}>
                      {fieldLabel(fields, ref)}, high to low
                    </option>,
                    <option key={`${ref}|asc`} value={`${ref}|asc`}>
                      {fieldLabel(fields, ref)}, low to high
                    </option>,
                  ])}
                </Select>
              </Field>
              <Field label="Row limit" htmlFor="item-limit">
                <Input
                  id="item-limit"
                  type="number"
                  min={1}
                  max={10000}
                  placeholder="1000"
                  value={item.limit ?? ""}
                  onChange={(e) => onChange({ ...item, limit: e.target.value ? Math.max(1, Math.min(10000, Number(e.target.value))) : undefined })}
                />
              </Field>
            </div>
          )}
        </div>
      )}

      <div className={SECTION}>
        <h3 className="mb-2 text-sm font-medium">Options</h3>
        <ConfigForm
          key={`${item.id}-${item.visual}`}
          schema={manifest.configSchema}
          value={item.config ?? {}}
          onChange={(config) => onChange({ ...item, config })}
          idPrefix={`cfg-${item.id}`}
        />
      </div>
      <div className={SECTION}>
        <Button variant="danger" onClick={onRemove}>
          Remove this visual
        </Button>
      </div>
    </div>
  );
}

// ---- filters ------------------------------------------------------------------------------

interface FiltersPanelProps {
  spec: Spec;
  fields: ModelField[]; // dimensions of every dataset the dashboard uses
  onChange: (filters: FilterControl[]) => void;
}

const TYPE_LABELS: Record<FilterType, string> = {
  multiSelect: "Pick several",
  select: "Pick one",
  search: "Search text",
  dateRange: "Date range",
};

export function FiltersPanel({ spec, fields, onChange }: FiltersPanelProps) {
  const filters = spec.filters ?? [];
  const dimensions = fields.filter((f) => f.kind === "dimension");
  const update = (index: number, patch: Partial<FilterControl>) =>
    onChange(filters.map((f, i) => (i === index ? { ...f, ...patch } : f)));
  const typeFor = (ref: string, wanted: FilterType): FilterType => {
    const isTime = dimensions.find((f) => f.ref === ref)?.type === "time";
    if (isTime) return "dateRange";
    return wanted === "dateRange" ? "multiSelect" : wanted;
  };
  return (
    <div className="space-y-3">
      <p className="text-xs text-zinc-500">
        A filter applies to every visual whose dataset has the field, or can reach it through a join.
      </p>
      {filters.map((filter, index) => {
        const isTime = dimensions.find((f) => f.ref === filter.field)?.type === "time";
        return (
          <fieldset key={filter.id} className="space-y-2 rounded-md border border-zinc-200 p-2 dark:border-zinc-800">
            <legend className="px-1 text-xs text-zinc-500">{filter.id}</legend>
            <Field label="Field" htmlFor={`f-field-${filter.id}`}>
              <Select
                id={`f-field-${filter.id}`}
                value={filter.field}
                onChange={(e) => update(index, { field: e.target.value, type: typeFor(e.target.value, filter.type), default: undefined })}
              >
                {dimensions.map((f) => (
                  <option key={f.ref} value={f.ref}>
                    {f.label} ({f.dataset})
                  </option>
                ))}
              </Select>
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Label" htmlFor={`f-label-${filter.id}`}>
                <Input id={`f-label-${filter.id}`} value={filter.label ?? ""} onChange={(e) => update(index, { label: e.target.value || undefined })} />
              </Field>
              <Field label="Control" htmlFor={`f-type-${filter.id}`}>
                <Select
                  id={`f-type-${filter.id}`}
                  value={filter.type}
                  disabled={isTime}
                  onChange={(e) => update(index, { type: e.target.value as FilterType, default: undefined })}
                >
                  {(isTime ? ["dateRange"] : ["multiSelect", "select", "search"]).map((type) => (
                    <option key={type} value={type}>
                      {TYPE_LABELS[type as FilterType]}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            {filter.type === "dateRange" && (
              <Field label="Starts as" htmlFor={`f-default-${filter.id}`}>
                <Select
                  id={`f-default-${filter.id}`}
                  value={(filter.default as { relative?: string } | undefined)?.relative ?? ""}
                  onChange={(e) => update(index, { default: e.target.value ? { relative: e.target.value } : undefined })}
                >
                  {RELATIVE_RANGES.map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Field>
            )}
            <Button variant="ghost" onClick={() => onChange(filters.filter((_, i) => i !== index))}>
              Remove filter
            </Button>
          </fieldset>
        );
      })}
      <Button
        disabled={dimensions.length === 0}
        title={dimensions.length === 0 ? "Add a visual with a dataset first" : undefined}
        onClick={() => {
          const first = dimensions[0]!;
          const id = newId("f", filters.map((f) => f.id).concat(spec.items.map((i) => i.id)));
          onChange([...filters, { id, type: first.type === "time" ? "dateRange" : "multiSelect", field: first.ref }]);
        }}
      >
        Add a filter
      </Button>
    </div>
  );
}

// ---- dashboard settings -----------------------------------------------------------------------

export function SettingsPanel({ spec, onChange }: { spec: Spec; onChange: (spec: Spec) => void }) {
  return (
    <div className="space-y-3">
      <Field label="Title" htmlFor="d-title" required>
        <Input id="d-title" value={spec.title} maxLength={200} onChange={(e) => onChange({ ...spec, title: e.target.value })} />
      </Field>
      <Field label="Description" htmlFor="d-description">
        <Input id="d-description" value={spec.description ?? ""} onChange={(e) => onChange({ ...spec, description: e.target.value || undefined })} />
      </Field>
      <Field label="Colour mode" htmlFor="d-mode" hint="Auto follows each viewer's system setting.">
        <Select
          id="d-mode"
          value={spec.theme?.mode ?? "auto"}
          onChange={(e) => onChange({ ...spec, theme: { ...spec.theme, mode: e.target.value as "auto" | "light" | "dark" } })}
        >
          <option value="auto">Auto</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </Select>
      </Field>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={spec.interactions?.crossFilter ?? false}
          onChange={(e) => onChange({ ...spec, interactions: { ...spec.interactions, crossFilter: e.target.checked } })}
        />
        Clicking a value in one visual filters the others
      </label>
    </div>
  );
}

// ---- history ----------------------------------------------------------------------------------

interface HistoryPanelProps {
  dashboardId: string;
  present: Spec;
  dirty: boolean;
  onRestored: (spec: Spec) => void;
}

export function HistoryPanel({ dashboardId, present, dirty, onRestored }: HistoryPanelProps) {
  const versions = useDashboardVersions(dashboardId);
  const restore = useRestoreDashboard(dashboardId);
  const [selected, setSelected] = useState<number | null>(null);
  const chosen = useDashboardVersion(dashboardId, selected);
  const lines = chosen.data
    ? diffLines(JSON.stringify(chosen.data.spec, null, 2), JSON.stringify(present, null, 2))
    : [];
  const changes = countChanges(lines);
  const details = restore.error instanceof ApiError ? restore.error.details : [];
  return (
    <div className="space-y-3">
      <ErrorNote error={versions.error ?? restore.error} />
      {details.map((d) => (
        <p key={d.path + d.message} className="text-xs text-red-700 dark:text-red-400">
          <code>{d.path}</code> {d.message}
        </p>
      ))}
      <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
        {versions.data?.map((version) => (
          <li key={version.version} className="py-1.5 text-sm">
            <button
              type="button"
              aria-pressed={selected === version.version}
              className={`w-full rounded px-1 text-left ${selected === version.version ? "bg-zinc-200/70 dark:bg-zinc-800" : "hover:bg-zinc-100 dark:hover:bg-zinc-800/60"}`}
              onClick={() => setSelected(selected === version.version ? null : version.version)}
            >
              <span className="font-medium">Version {version.version}</span>{" "}
              {version.current && <Badge>Latest saved</Badge>} {version.published && <Badge tone="good">Published</Badge>}
              <span className="block text-xs text-zinc-500">
                {version.authorName}, {timeAgo(version.createdAt)}
                {version.message && `: ${version.message}`}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {selected !== null && chosen.data && (
        <div>
          <p className="mb-1 text-xs text-zinc-600 dark:text-zinc-400" role="status">
            From version {selected} to what you see now: {changes.added} line{changes.added === 1 ? "" : "s"} added,{" "}
            {changes.removed} removed.
          </p>
          {changes.added + changes.removed > 0 && (
            <pre className="max-h-72 overflow-auto rounded-md bg-zinc-100 p-2 font-mono text-[11px] leading-4 dark:bg-zinc-950" aria-label={`Changes since version ${selected}`}>
              {withContext(lines).map((line, index) =>
                line === "gap" ? (
                  <span key={index} className="block text-zinc-400">
                    {"  ..."}
                  </span>
                ) : (
                  <span
                    key={index}
                    className={`block whitespace-pre-wrap ${line.kind === "added" ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200" : line.kind === "removed" ? "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200" : ""}`}
                  >
                    {line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}
                    {line.text}
                  </span>
                ),
              )}
            </pre>
          )}
          <Button
            className="mt-2"
            busy={restore.isPending}
            disabled={dirty}
            title={dirty ? "Save or undo your changes first" : undefined}
            onClick={() => restore.mutate(selected, { onSuccess: (saved) => { onRestored(saved.spec); setSelected(null); } })}
          >
            Restore version {selected}
          </Button>
          {dirty && <p className="mt-1 text-xs text-zinc-500">Save or undo your changes before restoring.</p>}
        </div>
      )}
    </div>
  );
}
