// Thin fetch wrapper. Every API error has the shape { error: { code, message, details } }.
// Paths are app-relative ("/api/config"); the base path is added here.
import { readCookie, url } from "./runtime";

export interface ErrorDetail {
  path?: string;
  message: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: ErrorDetail[] = [],
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-CSRF-Token"] = readCookie("opsis_csrf");
  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const response = await fetch(url(path), {
    method,
    headers,
    body: payload,
    credentials: "same-origin",
  });
  if (!response.ok) {
    let code = "http_error";
    let message = `Request failed (${response.status})`;
    let details: ErrorDetail[] = [];
    try {
      const parsed = (await response.json()) as {
        error?: { code?: string; message?: string; details?: ErrorDetail[] };
      };
      code = parsed.error?.code ?? code;
      message = parsed.error?.message ?? message;
      details = parsed.error?.details ?? [];
    } catch {
      // Not JSON: keep the generic message.
    }
    throw new ApiError(response.status, code, message, details);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const apiGet = <T>(path: string) => request<T>("GET", path);
export const apiPost = <T>(path: string, body?: unknown) => request<T>("POST", path, body);
export const apiPatch = <T>(path: string, body?: unknown) => request<T>("PATCH", path, body);
export const apiDelete = <T = void>(path: string) => request<T>("DELETE", path);

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const details = error.details.map((d) => (d.path ? `${d.path}: ${d.message}` : d.message));
    return [error.message, ...details].join(" ");
  }
  return error instanceof Error ? error.message : String(error);
}
