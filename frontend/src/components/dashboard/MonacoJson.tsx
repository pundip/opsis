// Monaco JSON editor, bundled locally (no CDN) so it works offline and under the
// strict CSP. Loaded lazily: it is only needed on the editor's JSON tab.
//
// Only the editor core, the JSON language and a handful of editing features are
// imported, not the full "editor.main" bundle with every language.
import * as monaco from "monaco-editor/editor";
import "monaco-editor/editor/browser/coreCommands";
import "monaco-editor/editor/contrib/bracketMatching/browser/bracketMatching";
import "monaco-editor/editor/contrib/clipboard/browser/clipboard";
import "monaco-editor/editor/contrib/comment/browser/comment";
import "monaco-editor/editor/contrib/contextmenu/browser/contextmenu";
import "monaco-editor/editor/contrib/cursorUndo/browser/cursorUndo";
import "monaco-editor/editor/contrib/find/browser/findController";
import "monaco-editor/editor/contrib/folding/browser/folding";
import "monaco-editor/editor/contrib/format/browser/formatActions";
import "monaco-editor/editor/contrib/gotoError/browser/gotoError";
import "monaco-editor/editor/contrib/hover/browser/hoverContribution";
import "monaco-editor/editor/contrib/indentation/browser/indentation";
import "monaco-editor/editor/contrib/linesOperations/browser/linesOperations";
import "monaco-editor/editor/contrib/multicursor/browser/multicursor";
import "monaco-editor/editor/contrib/suggest/browser/suggestController";
import "monaco-editor/editor/contrib/wordOperations/browser/wordOperations";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";
import { jsonDefaults } from "monaco-editor/language/json/monaco.contribution";
import { useEffect, useRef } from "react";

(globalThis as { MonacoEnvironment?: monaco.Environment }).MonacoEnvironment = {
  getWorker: (_id: string, label: string) => (label === "json" ? new JsonWorker() : new EditorWorker()),
};

interface Props {
  value: string;
  onChange: (value: string) => void;
  /** JSON Schemas by file name. The one named `main` validates the document. */
  schemas: Record<string, object>;
  main: string;
  dark: boolean;
  label: string;
}

export default function MonacoJson({ value, onChange, schemas, main, dark, label }: Props) {
  const element = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const changed = useRef(onChange);
  changed.current = onChange;

  useEffect(() => {
    // Schemas refer to each other by $id; all are supplied here, so nothing is fetched.
    jsonDefaults.setDiagnosticsOptions({
      validate: true,
      enableSchemaRequest: false,
      schemas: Object.entries(schemas).map(([name, schema]) => ({
        uri: (schema as { $id?: string }).$id ?? `opsis://schemas/${name}`,
        fileMatch: name === main ? ["*"] : [],
        schema,
      })),
    });
  }, [schemas, main]);

  useEffect(() => {
    const instance = monaco.editor.create(element.current!, {
      value,
      language: "json",
      automaticLayout: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      fontSize: 12,
      tabSize: 2,
      ariaLabel: label,
    });
    editor.current = instance;
    const subscription = instance.onDidChangeModelContent(() => changed.current(instance.getValue()));
    return () => {
      subscription.dispose();
      instance.dispose();
      editor.current = null;
    };
    // The editor is created once; later `value` changes are applied below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const instance = editor.current;
    if (instance && instance.getValue() !== value) instance.setValue(value);
  }, [value]);

  useEffect(() => {
    monaco.editor.setTheme(dark ? "vs-dark" : "vs");
  }, [dark]);

  return <div ref={element} className="h-full min-h-80 w-full overflow-hidden rounded-md border border-zinc-300 dark:border-zinc-700" />;
}
