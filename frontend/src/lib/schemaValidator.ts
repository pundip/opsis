// A JSON Schema validator for react-jsonschema-form that works under a strict
// Content-Security-Policy.
//
// rjsf's stock validator (@rjsf/validator-ajv8) compiles schemas with
// `new Function`, which `script-src 'self'` forbids. This one uses
// @cfworker/json-schema, an interpreter that never generates code. See ADR-018.
import { type OutputUnit, Validator } from "@cfworker/json-schema";
import {
  createErrorHandler,
  type RJSFSchema,
  type RJSFValidationError,
  toErrorSchema,
  unwrapErrorHandler,
  validationDataMerge,
  type ValidatorType,
} from "@rjsf/utils";

// Keywords that only say "something below me failed". The leaf error is reported instead.
const CONTAINERS = new Set([
  "properties", "items", "prefixItems", "additionalProperties", "patternProperties",
  "allOf", "$ref", "if", "then", "else", "dependentSchemas", "contains",
]);

export interface SchemaProblem {
  path: string; // JSON pointer, "/orientation"
  message: string;
}

/** Validate data against a JSON Schema (draft 2020-12). No code generation. */
export function validateAgainst(schema: object, data: unknown): SchemaProblem[] {
  let units: OutputUnit[];
  try {
    units = new Validator(schema as never, "2020-12", false).validate(data).errors;
  } catch (error) {
    return [{ path: "/", message: `The schema could not be used: ${String(error)}` }];
  }
  const seen = new Set<string>();
  const problems: SchemaProblem[] = [];
  for (const unit of units) {
    if (CONTAINERS.has(unit.keyword)) continue;
    let path = unit.instanceLocation.replace(/^#/, "") || "/";
    // Report a missing property at the property, where a form can show it.
    const missing = unit.keyword === "required" ? /"([^"]+)"/.exec(unit.error) : null;
    if (missing) {
      path = `${path === "/" ? "" : path}/${missing[1]}`;
      unit.error = "This is required.";
    }
    const key = `${path}|${unit.error}`;
    if (seen.has(key)) continue;
    seen.add(key);
    problems.push({ path, message: unit.error });
  }
  return problems;
}

function toRjsfErrors(schema: object, data: unknown): RJSFValidationError[] {
  return validateAgainst(schema, data).map(({ path, message }) => {
    const property = path === "/" ? "." : path.replace(/\//g, ".");
    return { property, message, stack: `${property} ${message}`.trim() };
  });
}

export const cspSafeValidator: ValidatorType = {
  validateFormData(formData, schema, customValidate, transformErrors) {
    let errors = toRjsfErrors(schema, formData);
    if (transformErrors) errors = transformErrors(errors);
    const result = { errors, errorSchema: toErrorSchema(errors) };
    if (!customValidate) return result;
    const handler = customValidate(formData, createErrorHandler(formData));
    return validationDataMerge(result, unwrapErrorHandler(handler));
  },
  isValid(schema: RJSFSchema, formData: unknown, rootSchema: RJSFSchema) {
    // Sub-schemas may point into the root's definitions.
    const withDefinitions = { $defs: rootSchema.$defs, definitions: rootSchema.definitions, ...schema };
    return validateAgainst(withDefinitions, formData).length === 0;
  },
  rawValidation(schema: RJSFSchema, formData?: unknown) {
    return { errors: toRjsfErrors(schema, formData) as never[] };
  },
};
