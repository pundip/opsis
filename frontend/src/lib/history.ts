// Undo and redo for the dashboard editor: a plain list of past, present and future states.
import { useCallback, useMemo, useState } from "react";

export interface History<T> {
  past: T[];
  present: T;
  future: T[];
}

const LIMIT = 100;

export const start = <T>(present: T): History<T> => ({ past: [], present, future: [] });

/** Record a new state. Identical states are ignored, and the redo list is cleared. */
export function push<T>(history: History<T>, next: T): History<T> {
  if (next === history.present) return history;
  return { past: [...history.past, history.present].slice(-LIMIT), present: next, future: [] };
}

export function undo<T>(history: History<T>): History<T> {
  const previous = history.past.at(-1);
  if (previous === undefined) return history;
  return {
    past: history.past.slice(0, -1),
    present: previous,
    future: [history.present, ...history.future],
  };
}

export function redo<T>(history: History<T>): History<T> {
  const [next, ...rest] = history.future;
  if (next === undefined) return history;
  return { past: [...history.past, history.present], present: next, future: rest };
}

export function useHistory<T>(initial: T) {
  const [history, setHistory] = useState(() => start(initial));
  const set = useCallback((next: T | ((current: T) => T)) => {
    setHistory((h) => push(h, typeof next === "function" ? (next as (c: T) => T)(h.present) : next));
  }, []);
  const undoIt = useCallback(() => setHistory(undo), []);
  const redoIt = useCallback(() => setHistory(redo), []);
  const reset = useCallback((value: T) => setHistory(start(value)), []);
  return useMemo(
    () => ({
      present: history.present,
      set,
      undo: undoIt,
      redo: redoIt,
      reset,
      canUndo: history.past.length > 0,
      canRedo: history.future.length > 0,
    }),
    [history, set, undoIt, redoIt, reset],
  );
}
