// The assistant panel in the editor: a conversation about the open dashboard.
// Replies stream in; each tool call is a step that can be opened to see what it
// sent and got back; each change it made can be undone with one click.
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import {
  type Entry,
  type Live,
  type Step,
  type Summary,
  NO_LIVE,
  applyEvent,
  sendToAssistant,
  useAssistant,
  useNewConversation,
  useStopAssistant,
} from "../../lib/assistant";
import type { Item } from "../../lib/dashboard";
import { Button, ErrorNote } from "../ui";

interface Props {
  dashboardId: string;
  items: Item[];
  selected: Item | null;
  /** The editor has unsaved changes: the assistant works on the saved version. */
  dirty: boolean;
  onUndo: (version: number) => void;
  undoing: boolean;
}

const PROVIDERS = { anthropic: "Claude", ollama: "Ollama" } as const;

function seconds(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

function UndoButton({ version, label, onUndo, undoing }: { version: number; label: string; onUndo: (v: number) => void; undoing: boolean }) {
  const [done, setDone] = useState(false);
  if (done) return <span className="text-xs text-zinc-500">Restored version {version}</span>;
  return (
    <Button
      className="!px-2 !py-0.5 text-xs"
      disabled={undoing}
      title={`Go back to version ${version}. Changes made after it are undone too; everything stays in the history.`}
      onClick={() => {
        onUndo(version);
        setDone(true);
      }}
    >
      {label}
    </Button>
  );
}

function StepRow({ step, onUndo, undoing }: { step: Step; onUndo: (v: number) => void; undoing: boolean }) {
  const failed = step.ok === false;
  const before = step.version?.before;
  return (
    <div className="rounded-md border border-zinc-200 text-sm dark:border-zinc-800" data-step={step.tool}>
      <details>
        <summary className="flex cursor-pointer list-none items-center gap-2 px-2 py-1.5">
          <span aria-hidden className={failed ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}>
            {failed ? "✕" : "✓"}
          </span>
          <span className="min-w-0 flex-1 truncate">
            {step.title}
            {failed && <span className="sr-only"> (failed)</span>}
          </span>
          {step.version?.after != null && <span className="text-xs text-zinc-500">Saved version {step.version.after}</span>}
          {step.ms !== undefined && <span className="text-xs text-zinc-500">{seconds(step.ms)}</span>}
        </summary>
        <div className="space-y-2 border-t border-zinc-200 p-2 dark:border-zinc-800">
          <p className="text-xs text-zinc-500">
            Tool <code className="font-mono">{step.tool}</code>
          </p>
          <pre className="max-h-48 overflow-auto rounded bg-zinc-50 p-2 font-mono text-xs whitespace-pre-wrap break-all dark:bg-zinc-950">{step.input}</pre>
          {step.output !== undefined && (
            <pre className={`max-h-48 overflow-auto rounded p-2 font-mono text-xs whitespace-pre-wrap break-all ${failed ? "bg-red-50 dark:bg-red-950/40" : "bg-zinc-50 dark:bg-zinc-950"}`}>
              {step.output}
            </pre>
          )}
        </div>
      </details>
      {/* Outside the details, so a change can be undone without opening the step. */}
      {before != null && (
        <div className="flex justify-end border-t border-zinc-200 px-2 py-1 dark:border-zinc-800">
          <UndoButton version={before} label="Undo" onUndo={onUndo} undoing={undoing} />
        </div>
      )}
    </div>
  );
}

const NOTICE = {
  info: "border-sky-300 bg-sky-50 dark:border-sky-900 dark:bg-sky-950/40",
  warning: "border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40",
  error: "border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40",
};

function DoneRow({ summary, onUndo, undoing }: { summary: Summary; onUndo: (v: number) => void; undoing: boolean }) {
  const cached = summary.cachedTokens ? `, ${summary.cachedTokens.toLocaleString()} cached` : "";
  const changed = summary.versionBefore != null && summary.versionAfter !== summary.versionBefore;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500">
      <span className="mr-auto">
        {summary.steps} {summary.steps === 1 ? "step" : "steps"}, {summary.inputTokens.toLocaleString()} tokens in{cached},{" "}
        {summary.outputTokens.toLocaleString()} out, {seconds(summary.ms)}
      </span>
      {changed && <UndoButton version={summary.versionBefore!} label="Undo all of this" onUndo={onUndo} undoing={undoing} />}
    </div>
  );
}

function EntryRow({ entry, items, onUndo, undoing }: { entry: Entry; items: Item[]; onUndo: (v: number) => void; undoing: boolean }) {
  switch (entry.kind) {
    case "request": {
      const about = entry.data.selected ? items.find((i) => i.id === entry.data.selected) : null;
      return (
        <div className="ml-8 rounded-lg bg-brand/10 px-3 py-2 text-sm whitespace-pre-wrap" data-role="request">
          {about && <p className="mb-1 text-xs text-zinc-500">About {about.title || about.id}</p>}
          {entry.data.text}
        </div>
      );
    }
    case "text":
      return (
        <p className="text-sm whitespace-pre-wrap" data-role="reply">
          {entry.data.text}
          {entry.data.partial && <span className="text-zinc-500"> (cut short)</span>}
        </p>
      );
    case "update":
      return <p className="text-xs text-zinc-500 italic whitespace-pre-wrap">{entry.data.text}</p>;
    case "step":
      return <StepRow step={entry.data} onUndo={onUndo} undoing={undoing} />;
    case "notice":
      return (
        <p role={entry.data.level === "error" ? "alert" : "note"} className={`rounded-md border px-3 py-2 text-sm ${NOTICE[entry.data.level]}`}>
          {entry.data.text}
        </p>
      );
    case "done":
      return <DoneRow summary={entry.data} onUndo={onUndo} undoing={undoing} />;
  }
}

function LiveRows({ live }: { live: Live }) {
  const blocks = Object.entries(live.blocks).sort(([a], [b]) => Number(a) - Number(b));
  return (
    <>
      {blocks.map(([index, block]) =>
        block.kind === "text" ? (
          <p key={index} className="text-sm whitespace-pre-wrap">{block.text}</p>
        ) : (
          <p key={index} className="text-xs text-zinc-500 italic whitespace-pre-wrap">{block.text}</p>
        ),
      )}
      <p role="status" className="flex items-center gap-2 text-xs text-zinc-500">
        <span aria-hidden className="size-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
        {live.running
          ? `Running: ${live.running.title}`
          : live.writing
            ? `Preparing a call to ${live.writing}`
            : live.thinking || blocks.length === 0
              ? "Thinking..."
              : "Writing..."}
      </p>
    </>
  );
}

export function AssistantPanel({ dashboardId, items, selected, dirty, onUndo, undoing }: Props) {
  const client = useQueryClient();
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [text, setText] = useState("");
  const state = useAssistant(dashboardId, live !== null);
  const polling = live === null && Boolean(state.data?.running);
  const stop = useStopAssistant(dashboardId);
  const fresh = useNewConversation(dashboardId);
  const log = useRef<HTMLDivElement>(null);

  const busy = live !== null || Boolean(state.data?.running);
  const entries = state.data?.entries ?? [];
  const stored = new Set(entries.map((entry) => entry.id));
  const shown = live ? [...entries, ...live.entries.filter((entry) => !stored.has(entry.id))] : entries;

  // Keep the newest entry in view, unless the person has scrolled up to read.
  const count = shown.length + Object.values(live?.blocks ?? {}).reduce((n, b) => n + b.text.length, 0);
  useEffect(() => {
    const box = log.current;
    if (box && box.scrollHeight - box.scrollTop - box.clientHeight < 200) box.scrollTop = box.scrollHeight;
  }, [count]);

  const send = async () => {
    const request = text.trim();
    if (!request || busy || dirty) return;
    setError(null);
    setText("");
    setLive({ ...NO_LIVE });
    let accepted = false;
    try {
      await sendToAssistant(dashboardId, { text: request, selectedItemId: selected?.id ?? null }, (event) => {
        accepted ||= event.event === "start";
        setLive((current) => applyEvent(current ?? NO_LIVE, event));
      });
    } catch (failure) {
      // Once accepted, the request goes on without this stream: the panel polls instead.
      if (!accepted) {
        setError(failure);
        setText(request);
      }
    } finally {
      await client.refetchQueries({ queryKey: ["assistant", dashboardId] });
      setLive(null);
    }
  };

  const provider = state.data ? `${PROVIDERS[state.data.provider]} (${state.data.model})` : "";
  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div className="flex items-center gap-2">
        <p className="mr-auto truncate text-xs text-zinc-500" title={provider}>
          {provider}
        </p>
        <Button className="!px-2 !py-0.5 text-xs" disabled={busy || shown.length === 0} busy={fresh.isPending} onClick={() => fresh.mutate()}>
          New conversation
        </Button>
      </div>
      <div ref={log} role="log" aria-label="Conversation with the assistant" className="min-h-0 flex-1 space-y-2 overflow-auto pr-1">
        {state.isPending && <p className="text-sm text-zinc-500">Loading...</p>}
        {state.data && shown.length === 0 && (
          <p className="text-sm text-zinc-500">
            Ask for a dashboard or a change, for example "Build a dashboard of gifts by fund and month", or ask a question about
            the data. Changes are saved as new versions you can undo.
          </p>
        )}
        {shown.map((entry) => (
          <EntryRow key={entry.id} entry={entry} items={items} onUndo={onUndo} undoing={undoing} />
        ))}
        {live && <LiveRows live={live} />}
        {polling && (
          <p role="status" className="text-xs text-zinc-500">
            The assistant is still working on this conversation...
          </p>
        )}
      </div>
      {state.data?.outdated && (
        <p className="text-xs text-zinc-500">The assistant now uses {provider}: your next request starts a new conversation.</p>
      )}
      <ErrorNote error={state.error ?? error ?? stop.error ?? fresh.error} />
      {dirty && !busy && <p className="text-xs text-amber-700 dark:text-amber-400">Save your changes first, so the assistant works on what you see.</p>}
      {selected && <p className="truncate text-xs text-zinc-500">About the selected visual: {selected.title || selected.id}</p>}
      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <textarea
          aria-label="Message to the assistant"
          className="min-h-20 w-full resize-y rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-950"
          placeholder="Ask the assistant..."
          maxLength={8000}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="flex items-center gap-2">
          <span className="mr-auto text-xs text-zinc-500">Enter to send, Shift+Enter for a new line</span>
          {busy ? (
            <Button busy={stop.isPending} onClick={() => stop.mutate()}>
              Stop
            </Button>
          ) : (
            <Button type="submit" variant="primary" disabled={!text.trim() || dirty} title={dirty ? "Save your changes first" : undefined}>
              Send
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
