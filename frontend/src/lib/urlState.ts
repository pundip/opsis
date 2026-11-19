// Filter values, cross-filters and drill levels in the URL (?s=...), so a filtered
// view can be bookmarked, reloaded and shared. Base64url JSON keeps it compact.
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { EMPTY_STATE, type ViewState } from "./dashboard";

const PARAM = "s";

function toBase64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(value: string): string {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64 + "=".repeat((4 - (base64.length % 4)) % 4));
  return new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)));
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Empty when there is nothing to keep. */
export function encodeViewState(state: ViewState): string | null {
  const empty = Object.keys(state.filters).length === 0 && state.cross.length === 0 && Object.keys(state.grains).length === 0;
  return empty ? null : toBase64Url(JSON.stringify(state));
}

/** Anything malformed is ignored rather than breaking the page. */
export function decodeViewState(value: string | null): ViewState {
  if (!value) return EMPTY_STATE;
  try {
    const parsed: unknown = JSON.parse(fromBase64Url(value));
    if (!isRecord(parsed)) return EMPTY_STATE;
    return {
      filters: isRecord(parsed.filters) ? parsed.filters : {},
      cross: Array.isArray(parsed.cross) ? (parsed.cross.filter(isRecord) as unknown as ViewState["cross"]) : [],
      grains: isRecord(parsed.grains) ? (parsed.grains as ViewState["grains"]) : {},
    };
  } catch {
    return EMPTY_STATE;
  }
}

export function useUrlViewState(): [ViewState, (next: ViewState) => void] {
  const [params, setParams] = useSearchParams();
  const encoded = params.get(PARAM);
  const state = useMemo(() => decodeViewState(encoded), [encoded]);
  const setState = useCallback(
    (next: ViewState) =>
      setParams(
        (current) => {
          const updated = new URLSearchParams(current);
          const value = encodeViewState(next);
          if (value) updated.set(PARAM, value);
          else updated.delete(PARAM);
          return updated;
        },
        { replace: true },
      ),
    [setParams],
  );
  return [state, setState];
}
