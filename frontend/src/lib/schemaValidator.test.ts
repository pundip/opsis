import type { RJSFSchema } from "@rjsf/utils";
import { describe, expect, it } from "vitest";
import { cspSafeValidator, validateAgainst } from "./schemaValidator";

const schema = {
  type: "object",
  additionalProperties: false,
  required: ["orientation"],
  properties: {
    orientation: { enum: ["vertical", "horizontal"], default: "vertical" },
    stacked: { type: "boolean" },
    limit: { type: "integer", minimum: 1, maximum: 50 },
    rules: {
      type: "array",
      items: {
        type: "object",
        required: ["field"],
        properties: { field: { type: "string", minLength: 1 }, color: { type: "string" } },
      },
    },
  },
} as RJSFSchema;

describe("validateAgainst", () => {
  it("accepts valid data", () => {
    expect(validateAgainst(schema, { orientation: "horizontal", stacked: true, limit: 5 })).toEqual([]);
  });

  it("reports leaf problems with JSON pointer paths", () => {
    const problems = validateAgainst(schema, {
      orientation: "diagonal",
      stacked: "yes",
      limit: 500,
      rules: [{ field: "a" }, { color: "red" }],
      extra: 1,
    });
    const paths = problems.map((p) => p.path).sort();
    expect(paths).toEqual(["/extra", "/limit", "/orientation", "/rules/1/field", "/stacked"]);
    expect(problems.every((p) => p.message.length > 0)).toBe(true);
  });

  it("reports a missing required property at that property", () => {
    expect(validateAgainst(schema, {})).toEqual([
      { path: "/orientation", message: "This is required." },
    ]);
  });

  it("does not throw on a broken schema", () => {
    expect(validateAgainst({ type: "object", properties: { a: { $ref: "#/nope" } } }, { a: 1 })).toHaveLength(1);
  });
});

describe("cspSafeValidator", () => {
  it("gives rjsf an error list and an error schema", () => {
    const result = cspSafeValidator.validateFormData({ orientation: "x", limit: 0 }, schema);
    expect(result.errors.map((e) => e.property).sort()).toEqual([".limit", ".orientation"]);
    expect(Object.keys(result.errorSchema).sort()).toEqual(["limit", "orientation"]);
  });

  it("answers isValid for sub-schemas that use root definitions", () => {
    const root = { $defs: { size: { type: "integer", minimum: 1 } } } as RJSFSchema;
    expect(cspSafeValidator.isValid({ $ref: "#/$defs/size" }, 3, root)).toBe(true);
    expect(cspSafeValidator.isValid({ $ref: "#/$defs/size" }, 0, root)).toBe(false);
  });

  it("never needs code generation", () => {
    const original = globalThis.Function;
    // Simulate the CSP: any attempt to build a function from a string fails.
    globalThis.Function = function () {
      throw new Error("blocked by CSP");
    } as never;
    try {
      expect(validateAgainst(schema, { orientation: "vertical" })).toEqual([]);
    } finally {
      globalThis.Function = original;
    }
  });
});
