// Asks before unsaved work is left behind. Links and the Back button inside the app go
// through the router, which waits here for an answer; reloading, closing the tab and
// leaving the app get the browser's own warning.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { type Blocker, type BlockerFunction, useBlocker } from "react-router-dom";
import { Button } from "./ui";

function LeaveDialog({ onStay, onLeave }: { onStay: () => void; onLeave: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  const warning = useId();
  useEffect(() => {
    // Modal: the page behind cannot be used. The first button, Stay, gets the focus.
    if (!dialog.current?.open) dialog.current?.showModal();
  }, []);
  // Answer at once: the dialog's close event comes later, and by then the router may be
  // asking again.
  const stay = () => {
    onStay();
    dialog.current?.close(); // gives the focus back to where it was
  };
  return (
    <dialog
      ref={dialog}
      role="alertdialog"
      aria-labelledby={title}
      aria-describedby={warning}
      onCancel={onStay} // Escape
      onClose={onStay} // in case the browser closes it without a cancel event
      className="m-auto w-full max-w-sm rounded-lg border border-zinc-200 bg-white p-5 text-zinc-900 shadow-xl backdrop:bg-black/40 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
    >
      <h2 id={title} className="text-base font-semibold">
        Leave without saving?
      </h2>
      <p id={warning} className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
        Your unsaved changes will be lost.
      </p>
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <Button variant="primary" onClick={stay}>
          Stay
        </Button>
        <Button variant="danger" onClick={onLeave}>
          Leave without saving
        </Button>
      </div>
    </dialog>
  );
}

/** Render it on a page with unsaved work. One at a time: the router supports one. */
export function LeaveGuard({ when }: { when: boolean }) {
  // Another path means another page. The query and the fragment stay on this one.
  const blocker = useBlocker(
    useCallback<BlockerFunction>(
      ({ currentLocation, nextLocation }) => when && currentLocation.pathname !== nextLocation.pathname,
      [when],
    ),
  );

  useEffect(() => {
    if (!when) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [when]);

  // One answer for each question: the router throws if told twice to go on, and a late
  // event can arrive while this component still holds the question already answered.
  const answered = useRef<Blocker | null>(null);
  const answer = useCallback(
    (leave: boolean) => {
      if (blocker.state !== "blocked" || answered.current === blocker) return;
      answered.current = blocker;
      if (leave) blocker.proceed();
      else blocker.reset();
    },
    [blocker],
  );

  // A save that finishes while the question is open leaves nothing to lose: go on.
  useEffect(() => {
    if (!when) answer(true);
  }, [when, answer]);

  // Each question gets a dialog of its own. React can go from one question straight to
  // the next, and a dialog closed for the first would stay closed for the second.
  const [shown, setShown] = useState({ blocker, number: 0 });
  if (shown.blocker !== blocker) setShown({ blocker, number: shown.number + 1 });

  if (!when || blocker.state !== "blocked") return null;
  return <LeaveDialog key={shown.number} onStay={() => answer(false)} onLeave={() => answer(true)} />;
}
