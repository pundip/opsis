// The YAML tab: the model (the part you may use) as one YAML document, the same one
// the canvas draws. Check runs the server's validation; Save saves it as a version.
import { Suspense, lazy, useEffect, useState } from "react";
import { url } from "../../lib/runtime";
import { useChartTheme } from "../../lib/theme";
import { type WorkspaceModel, usePutModel, useValidateModel } from "../../lib/workspaceModel";
import { LeaveGuard } from "../LeaveGuard";
import { Button, Card, Input } from "../ui";
import { ProblemList, SaveError, WarningList } from "./lists";

const MonacoYaml = lazy(() => import("./MonacoYaml"));

export function ModelYaml({ model }: { model: WorkspaceModel }) {
  const dark = useChartTheme(undefined).mode === "dark";
  // What the text started from: the yaml and its version.
  const [loaded, setLoaded] = useState({ yaml: model.yaml, version: model.version });
  const [text, setText] = useState(model.yaml);
  const [message, setMessage] = useState("");
  const save = usePutModel();
  const validate = useValidateModel();
  const dirty = text !== loaded.yaml;
  const behind = dirty && model.version !== loaded.version;

  // A change saved elsewhere (the canvas, someone else) shows here unless you are editing.
  useEffect(() => {
    if (!dirty) {
      setLoaded({ yaml: model.yaml, version: model.version });
      setText(model.yaml);
    }
    // Only when the model changes: `dirty` is read at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model.yaml, model.version]);

  const checked = validate.variables?.yaml === text ? validate.data : undefined;

  return (
    <Card className="p-4">
      <LeaveGuard when={dirty} />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="mr-auto text-base font-semibold">The model as YAML</h2>
        <a href={url("/api/model/export")} className="text-sm text-brand underline" download="workspace-model.yml">
          Download workspace-model.yml
        </a>
      </div>
      <Suspense fallback={<p className="h-[32rem] text-sm text-zinc-500">Loading the editor.</p>}>
        <MonacoYaml
          value={text}
          onChange={(value) => {
            setText(value);
            save.reset();
          }}
          dark={dark}
          label="The workspace model in YAML"
        />
      </Suspense>
      <div className="mt-3 space-y-3">
        {behind && (
          <p className="rounded-md border border-zinc-200 bg-zinc-50 p-2 text-sm dark:border-zinc-800 dark:bg-zinc-950">
            The model changed since you started editing (it is now version {model.version}). Saving applies your
            changes to it, unless they touch the same places.
          </p>
        )}
        {checked && <ProblemList label="Problems from the check" problems={checked.problems} />}
        {save.error ? <SaveError error={save.error} /> : <SaveError error={validate.error} />}
        {checked?.valid && (
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            {dirty ? "The model is valid. Save it to use it." : "The model is valid."}
          </p>
        )}
        {checked && checked.existing.length > 0 && (
          <ProblemList label="Problems the model already has" problems={checked.existing} />
        )}
        {checked && <WarningList label="Warnings from the check" warnings={checked.warnings} />}
        {save.isSuccess && !dirty && (
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
            Saved as version {save.data.version}.
          </p>
        )}
        {dirty && (
          <Input
            aria-label="Note for the version history (optional)"
            placeholder="Note for the version history (optional)"
            maxLength={500}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
          />
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            busy={save.isPending}
            disabled={!dirty}
            onClick={() => {
              validate.reset(); // the save checks the text again
              save.mutate(
                { yaml: text, baseVersion: loaded.version, message: message || undefined },
                {
                  onSuccess: (saved) => {
                    setLoaded({ yaml: saved.yaml, version: saved.version });
                    setText(saved.yaml);
                    setMessage("");
                  },
                },
              );
            }}
          >
            Save model
          </Button>
          <Button busy={validate.isPending} onClick={() => validate.mutate({ yaml: text, baseVersion: loaded.version })}>
            Check
          </Button>
          <Button
            variant="ghost"
            disabled={!dirty}
            onClick={() => {
              setLoaded({ yaml: model.yaml, version: model.version });
              setText(model.yaml);
              validate.reset();
              save.reset();
            }}
          >
            Discard changes
          </Button>
        </div>
      </div>
    </Card>
  );
}
