// Monaco for the workspace model's YAML, bundled locally (no CDN) like the dashboard
// JSON editor. Highlighting only: Check runs the server's validation, which also
// knows the data (ADR-039).
import * as monaco from "monaco-editor/editor";
import "monaco-editor/editor/browser/coreCommands";
import "monaco-editor/editor/contrib/bracketMatching/browser/bracketMatching";
import "monaco-editor/editor/contrib/clipboard/browser/clipboard";
import "monaco-editor/editor/contrib/comment/browser/comment";
import "monaco-editor/editor/contrib/contextmenu/browser/contextmenu";
import "monaco-editor/editor/contrib/cursorUndo/browser/cursorUndo";
import "monaco-editor/editor/contrib/find/browser/findController";
import "monaco-editor/editor/contrib/folding/browser/folding";
import "monaco-editor/editor/contrib/indentation/browser/indentation";
import "monaco-editor/editor/contrib/linesOperations/browser/linesOperations";
import "monaco-editor/editor/contrib/multicursor/browser/multicursor";
import "monaco-editor/editor/contrib/wordOperations/browser/wordOperations";
import "monaco-editor/languages/definitions/yaml/register";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import { useEffect, useRef } from "react";

// The JSON editor sets a worker for JSON too; either serves YAML, which has none.
const scope = globalThis as { MonacoEnvironment?: monaco.Environment };
scope.MonacoEnvironment ??= { getWorker: () => new EditorWorker() };

interface Props {
  value: string;
  onChange: (value: string) => void;
  dark: boolean;
  label: string;
  readOnly?: boolean;
}

export default function MonacoYaml({ value, onChange, dark, label, readOnly }: Props) {
  const element = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const changed = useRef(onChange);
  changed.current = onChange;
  // True while the page sets the text (a saved version, a discard): that is not an edit.
  const applying = useRef(false);

  useEffect(() => {
    const instance = monaco.editor.create(element.current!, {
      value,
      language: "yaml",
      automaticLayout: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      fontSize: 12,
      tabSize: 2,
      insertSpaces: true,
      ariaLabel: label,
      readOnly,
    });
    editor.current = instance;
    const subscription = instance.onDidChangeModelContent(() => {
      if (!applying.current) changed.current(instance.getValue());
    });
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
    if (!instance || instance.getValue() === value) return;
    applying.current = true;
    try {
      instance.setValue(value);
    } finally {
      applying.current = false;
    }
  }, [value]);

  useEffect(() => {
    editor.current?.updateOptions({ readOnly });
  }, [readOnly]);

  useEffect(() => {
    monaco.editor.setTheme(dark ? "vs-dark" : "vs");
  }, [dark]);

  return (
    <div
      ref={element}
      data-testid="model-yaml-editor"
      className="h-[32rem] w-full overflow-hidden rounded-md border border-zinc-300 dark:border-zinc-700"
    />
  );
}
