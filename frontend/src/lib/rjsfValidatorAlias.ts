// @rjsf/core imports "@rjsf/validator-ajv8" from a test helper in its entry
// point. That package compiles code with `new Function`, which our CSP forbids,
// and it is not installed. vite.config.ts aliases the import to this file, so
// anything that asks for the stock validator gets the CSP-safe one.
export { cspSafeValidator as default } from "./schemaValidator";
