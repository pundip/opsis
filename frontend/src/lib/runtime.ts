// Runtime config injected into index.html by the backend (brief 3.1).
// The same build runs at "/" and at "/opsis", so nothing here is known at build time.

interface RuntimeConfig {
  basePath: string;
  appName?: string;
  version?: string;
}

function readRuntimeConfig(): RuntimeConfig {
  if (typeof document === "undefined") return { basePath: "" };
  const node = document.getElementById("opsis-config");
  if (!node?.textContent) return { basePath: "" }; // Vite dev server: served at the root
  try {
    const parsed = JSON.parse(node.textContent) as Partial<RuntimeConfig>;
    return { ...parsed, basePath: parsed.basePath ?? "" };
  } catch {
    return { basePath: "" };
  }
}

export const runtime = readRuntimeConfig();

/** Prefix an app-relative path ("/api/config") with the base path. */
export function url(path: string): string {
  return `${runtime.basePath}${path}`;
}

/** Keys for non-sensitive browser storage. Auth never uses browser storage. */
export function storageKey(name: string): string {
  return `opsis:${name}`;
}

export function readCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const prefix = `${name}=`;
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) return decodeURIComponent(part.slice(prefix.length));
  }
  return "";
}
