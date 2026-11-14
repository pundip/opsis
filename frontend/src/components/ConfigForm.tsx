// A settings form generated from a JSON Schema, using react-jsonschema-form
// with the CSP-safe validator. Used for visual config panels.
import Form from "@rjsf/core";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { cspSafeValidator } from "../lib/schemaValidator";

interface Props {
  schema: object;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  uiSchema?: UiSchema;
  idPrefix?: string;
}

/** Long text fields (a text tile's Markdown) get a multi-line box. */
function longTextFields(schema: object): UiSchema {
  const properties = (schema as { properties?: Record<string, { type?: string; maxLength?: number }> }).properties ?? {};
  const ui: UiSchema = {};
  for (const [name, property] of Object.entries(properties)) {
    if (property.type === "string" && (property.maxLength ?? 0) > 500) {
      ui[name] = { "ui:widget": "textarea", "ui:options": { rows: 10 } };
    }
  }
  return ui;
}

export function ConfigForm({ schema, value, onChange, uiSchema, idPrefix = "cfg" }: Props) {
  return (
    <div className="rjsf-opsis">
      <Form
        schema={schema as RJSFSchema}
        uiSchema={{ "ui:submitButtonOptions": { norender: true }, ...longTextFields(schema), ...uiSchema }}
        formData={value}
        validator={cspSafeValidator}
        idPrefix={idPrefix}
        liveValidate
        showErrorList={false}
        onChange={(event) => onChange((event.formData ?? {}) as Record<string, unknown>)}
      />
    </div>
  );
}
