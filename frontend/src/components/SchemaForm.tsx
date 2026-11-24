// Renders a connector's config_schema() as a form.
//
// Supports what connector schemas use: text, number, boolean, enum, secret
// ("x-secret") and file upload ("x-upload") fields, and text read from a local
// file ("x-file-text"), such as a key file, which never leaves the browser except
// as the field's value. Validation is done by the
// backend, which returns one message per JSON path; they are shown under the
// matching field. See ADR-018 for why this is not react-jsonschema-form.
import { useState } from "react";
import { apiPost, errorText, type ErrorDetail } from "../lib/api";
import type { ConfigSchema, JsonSchemaProperty } from "../lib/data";
import { formatBytes } from "../lib/format";
import { Field, Input, Select } from "./ui";

type Values = Record<string, unknown>;

interface Props {
  schema: ConfigSchema;
  values: Values;
  onChange: (values: Values) => void;
  savedSecrets?: string[]; // secret fields that already have a stored value
  details?: ErrorDetail[]; // backend validation messages, paths like /config/host
  idPrefix: string;
  maxUploadMb: number;
}

/** Default values declared in a schema. */
export function schemaDefaults(schema: ConfigSchema): Values {
  const values: Values = {};
  for (const [name, property] of Object.entries(schema.properties)) {
    if (property.default !== undefined) values[name] = property.default;
  }
  return values;
}

/** Reads a small local file (a key file) into a field, in the browser. */
function FileTextButton(props: { id: string; accept: string; onText: (text: string, name: string) => void }) {
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <span className="mt-1 flex items-center gap-2 text-xs">
      <label htmlFor={props.id} className="cursor-pointer text-brand underline">
        Load from a file
      </label>
      <input
        id={props.id}
        type="file"
        accept={props.accept}
        className="sr-only"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = "";
          if (!file) return;
          if (file.size > 64 * 1024) {
            setProblem("That file is too large to be a key file.");
            return;
          }
          setProblem(null);
          void file.text().then((text) => props.onText(text.trim(), file.name));
        }}
      />
      {problem && <span className="text-red-700 dark:text-red-400">{problem}</span>}
    </span>
  );
}

function UploadField(props: {
  id: string;
  value: string;
  maxUploadMb: number;
  onUploaded: (file: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const current = props.value ? props.value.split("/").slice(1).join("/") : "";

  async function send(file: File | undefined) {
    if (!file) return;
    setProblem("");
    if (file.size > props.maxUploadMb * 1024 * 1024) {
      setProblem(`The file is ${formatBytes(file.size)}. The limit is ${props.maxUploadMb} MB.`);
      return;
    }
    setBusy(true);
    try {
      const body = new FormData();
      body.append("file", file);
      const result = await apiPost<{ file: string }>("/api/uploads", body);
      props.onUploaded(result.file);
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <input
        id={props.id}
        type="file"
        accept=".csv,.tsv,.txt,.xlsx,.xlsm"
        disabled={busy}
        onChange={(event) => void send(event.target.files?.[0])}
        className="block w-full text-sm file:mr-3 file:rounded-md file:border file:border-zinc-300 file:bg-white file:px-3 file:py-1.5 file:text-sm file:font-medium dark:file:border-zinc-700 dark:file:bg-zinc-900 dark:file:text-zinc-100"
      />
      <p className="mt-1 text-xs text-zinc-500" aria-live="polite">
        {busy ? "Uploading..." : current ? `Uploaded: ${current}` : "No file uploaded yet."}
      </p>
      {problem && (
        <p role="alert" className="mt-1 text-xs text-red-700 dark:text-red-400">
          {problem}
        </p>
      )}
    </div>
  );
}

export function SchemaForm(props: Props) {
  const { schema, values, onChange, savedSecrets = [], details = [], idPrefix } = props;
  const required = new Set(schema.required ?? []);
  const set = (name: string, value: unknown) => onChange({ ...values, [name]: value });
  const errorFor = (name: string) =>
    details
      .filter((d) => d.path === `/config/${name}`)
      .map((d) => d.message)
      .join(" ");
  const general = details.filter((d) => d.path === "/config/" || !d.path?.startsWith("/config/"));

  function control(name: string, property: JsonSchemaProperty, id: string) {
    const value = values[name];
    if (property["x-upload"]) {
      return (
        <UploadField
          id={id}
          value={typeof value === "string" ? value : ""}
          maxUploadMb={props.maxUploadMb}
          onUploaded={(file) => set(name, file)}
        />
      );
    }
    if (property.type === "boolean") {
      return (
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => set(name, event.target.checked)}
          className="size-4"
        />
      );
    }
    if (property.enum) {
      return (
        <Select id={id} value={String(value ?? "")} onChange={(e) => set(name, e.target.value)}>
          {property.enum.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
      );
    }
    if (property.type === "integer" || property.type === "number") {
      return (
        <Input
          id={id}
          type="number"
          min={property.minimum}
          max={property.maximum}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => set(name, e.target.value === "" ? undefined : Number(e.target.value))}
        />
      );
    }
    const secret = Boolean(property["x-secret"]);
    const input = (
      <Input
        id={id}
        type={secret ? "password" : "text"}
        autoComplete={secret ? "new-password" : "off"}
        placeholder={secret && savedSecrets.includes(name) ? "Saved. Leave blank to keep it." : ""}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => set(name, e.target.value)}
      />
    );
    if (!property["x-file-text"]) return input;
    return (
      <>
        {input}
        <FileTextButton id={`${id}-file`} accept={property["x-file-text"]} onText={(text) => set(name, text)} />
      </>
    );
  }

  return (
    <div className="space-y-4">
      {Object.entries(schema.properties).map(([name, property]) => {
        const id = `${idPrefix}-${name}`;
        return (
          <Field
            key={name}
            label={property.title ?? name}
            htmlFor={id}
            hint={property.description}
            error={errorFor(name)}
            required={required.has(name) && !(property["x-secret"] && savedSecrets.includes(name))}
          >
            {control(name, property, id)}
          </Field>
        );
      })}
      {general.map((detail) => (
        <p key={detail.message} role="alert" className="text-xs text-red-700 dark:text-red-400">
          {detail.message}
        </p>
      ))}
    </div>
  );
}
