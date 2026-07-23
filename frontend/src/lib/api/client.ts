import { ApiError, NetworkError, toProblem } from "./problem";
import { tokenStore } from "./token-store";

/**
 * The single fetch client every data hook goes through.
 *
 * Responsibilities, in order:
 *  1. Prefix `/api/v1`, serialise JSON, attach the `Authorization: Bearer` header
 *     from the in-memory token store.
 *  2. Attach the `X-CSRF-Token` double-submit header on mutating verbs (the
 *     backend requires it for cookie-authenticated mutations; it is harmless on
 *     Bearer-authenticated ones — see docs/ARCHITECTURE.md).
 *  3. On a 401 that is not itself an auth call, transparently refresh the access
 *     token once and replay the original request. Concurrent 401s share a single
 *     in-flight refresh (single-flight) so a burst does not rotate the refresh
 *     token N times and trip the family-reuse revocation.
 *  4. Map every failure to a typed error: {@link ApiError} for an HTTP problem,
 *     {@link NetworkError} for a transport failure.
 */

const API_PREFIX = "/api/v1";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface RequestOptions {
  method?: string;
  /** JSON-serialisable body. Omit for GET. */
  body?: unknown;
  /** Query-string params; `undefined`/`null` values are dropped. */
  params?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
  /** Set to true for `/auth/*` calls that must not trigger the refresh retry. */
  skipAuthRetry?: boolean;
  /** Override parsing (e.g. streamed downloads/exports handled by the caller). */
  raw?: boolean;
}

/** A refresh in progress, shared by every request that 401s while it runs. */
let refreshInFlight: Promise<boolean> | null = null;

function buildUrl(path: string, params?: RequestOptions["params"]): string {
  const url = `${API_PREFIX}${path}`;
  if (!params) return url;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.append(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `${url}?${qs}` : url;
}

async function parseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return null;
  }
  if (contentType.includes("application/json") || contentType.includes("+json")) {
    return response.json();
  }
  return response.text();
}

/**
 * Attempt a single token refresh. Returns true on success. All concurrent
 * callers await the same promise; the first one clears it when settled.
 */
async function refreshAccessToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${API_PREFIX}/auth/refresh`, {
        method: "POST",
        credentials: "same-origin",
        headers: csrfHeaders({}),
      });
      if (!response.ok) return false;
      const data = (await response.json()) as { access_token: string; csrf_token?: string };
      if (data.csrf_token) {
        tokenStore.set(data.access_token, data.csrf_token);
      } else {
        tokenStore.setAccessToken(data.access_token);
      }
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

function csrfHeaders(base: Record<string, string>): Record<string, string> {
  const csrf = tokenStore.getCsrfToken();
  return csrf ? { ...base, "X-CSRF-Token": csrf } : base;
}

function authHeaders(base: Record<string, string>): Record<string, string> {
  const token = tokenStore.getAccessToken();
  return token ? { ...base, Authorization: `Bearer ${token}` } : base;
}

async function execute(path: string, options: RequestOptions): Promise<Response> {
  const method = (options.method ?? "GET").toUpperCase();
  let headers: Record<string, string> = { Accept: "application/json" };

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  headers = authHeaders(headers);
  if (MUTATING_METHODS.has(method)) {
    headers = csrfHeaders(headers);
  }

  const init: RequestInit = {
    method,
    headers,
    credentials: "same-origin",
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }
  if (options.signal) {
    init.signal = options.signal;
  }

  try {
    return await fetch(buildUrl(path, options.params), init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new NetworkError("The request could not reach the server.", error);
  }
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await execute(path, options);

  // Transparent refresh-and-replay on a single 401.
  if (response.status === 401 && !options.skipAuthRetry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      response = await execute(path, options);
    } else {
      tokenStore.clear();
    }
  }

  if (options.raw) {
    if (!response.ok) {
      const body = await parseBody(response).catch(() => null);
      throw new ApiError(
        toProblem(response.status, body, response.statusText),
        response.headers.get("X-Correlation-ID"),
      );
    }
    return response as unknown as T;
  }

  const body = await parseBody(response).catch(() => null);

  if (!response.ok) {
    throw new ApiError(
      toProblem(response.status, body, response.statusText),
      response.headers.get("X-Correlation-ID"),
    );
  }

  return body as T;
}

/** Convenience verbs. */
export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...options, method: "POST", body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...options, method: "PUT", body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...options, method: "PATCH", body }),
  delete: <T>(path: string, options?: Omit<RequestOptions, "method">) =>
    apiRequest<T>(path, { ...options, method: "DELETE" }),
};
