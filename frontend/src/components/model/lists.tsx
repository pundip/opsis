// Problems stop a save; warnings save with the model but may give surprising numbers
// or access. Both point at the place in the model they are about.
import type { ReactNode } from "react";
import { ApiError } from "../../lib/api";
import type { ModelWarning, Problem } from "../../lib/semantic";
import { Button, ErrorNote } from "../ui";

export function ProblemList({ problems, label }: { problems: Problem[]; label?: string }) {
  if (problems.length === 0) return null;
  return (
    <ul
      role="alert"
      aria-label={label}
      className="space-y-1 rounded-md border border-red-200 bg-red-50 p-3 text-sm wrap-anywhere dark:border-red-900 dark:bg-red-950"
    >
      {problems.map((problem) => (
        <li key={problem.path + problem.message} className="text-red-800 dark:text-red-300">
          <code className="font-mono text-xs">{problem.path}</code> {problem.message}
        </li>
      ))}
    </ul>
  );
}

export function WarningList({ warnings, label }: { warnings: ModelWarning[]; label: string }) {
  if (warnings.length === 0) return null;
  return (
    <ul
      aria-label={label}
      className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm wrap-anywhere dark:border-amber-900 dark:bg-amber-950"
    >
      {warnings.map((warning) => (
        <li key={warning.code + warning.path + warning.message} className="text-amber-900 dark:text-amber-200">
          <code className="font-mono text-xs">{warning.path}</code> {warning.message}
        </li>
      ))}
    </ul>
  );
}

/** The problems an API error lists, or the error itself when it lists none. */
export function SaveError({ error }: { error: unknown }) {
  const problems = error instanceof ApiError ? (error.details as Problem[]) : [];
  if (problems.length) return <ProblemList problems={problems} />;
  return <ErrorNote error={error} />;
}

export function SidePanel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <aside
      aria-label={title}
      className="flex max-h-full min-h-0 w-full flex-col overflow-hidden rounded-lg border border-zinc-200 bg-white lg:w-[27rem] lg:shrink-0 dark:border-zinc-800 dark:bg-zinc-900"
    >
      <div className="flex items-center gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <h2 className="mr-auto text-base font-semibold">{title}</h2>
        <Button variant="ghost" aria-label={`Close: ${title}`} onClick={onClose}>
          Close
        </Button>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">{children}</div>
    </aside>
  );
}
