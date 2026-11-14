// Monaco's package exports its internal modules without type declarations.
// These are the few we import directly (see components/dashboard/MonacoJson.tsx).
declare module "monaco-editor/editor/browser/coreCommands";
declare module "monaco-editor/editor/contrib/*";

declare module "monaco-editor/language/json/monaco.contribution" {
  export const jsonDefaults: {
    setDiagnosticsOptions(options: {
      validate?: boolean;
      enableSchemaRequest?: boolean;
      schemas?: { uri: string; fileMatch?: string[]; schema?: object }[];
    }): void;
  };
}
