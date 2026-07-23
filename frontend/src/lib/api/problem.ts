import type { ProblemDetail } from "@/types/api";

/**
 * An API call that resolved to a non-2xx response. Carries the parsed RFC 9457
 * problem document so callers (and the global query error boundary) can render
 * `title`/`detail` and branch on `status` without re-parsing.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetail;
  readonly correlationId: string | null;

  constructor(problem: ProblemDetail, correlationId: string | null) {
    super(problem.detail || problem.title || `Request failed (${problem.status})`);
    this.name = "ApiError";
    this.status = problem.status;
    this.problem = problem;
    this.correlationId = correlationId;
  }

  /** Field-level validation errors, if the problem carried any. */
  get fieldErrors(): Record<string, string[]> {
    return this.problem.errors ?? {};
  }

  /** True for the auth-expiry case the client transparently retries once. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }
}

/**
 * A transport-level failure: the request never produced an HTTP response
 * (network down, DNS, TLS, aborted). Distinct from {@link ApiError} because the
 * UI treats "never reached the server" differently from "server said no".
 */
export class NetworkError extends Error {
  readonly cause?: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "NetworkError";
    this.cause = cause;
  }
}

/**
 * Coerce an arbitrary error response body into a ProblemDetail. The backend
 * always emits RFC 9457, but a proxy (nginx) or an unexpected 5xx may return
 * HTML or plain text, so we synthesise a problem rather than throwing while
 * handling an error.
 */
export function toProblem(status: number, body: unknown, statusText: string): ProblemDetail {
  if (
    body &&
    typeof body === "object" &&
    "title" in body &&
    typeof (body as { title: unknown }).title === "string"
  ) {
    const problem = body as ProblemDetail;
    // Trust the server's status if present, else fall back to the HTTP status.
    return { ...problem, status: problem.status || status };
  }
  return {
    type: "about:blank",
    title: statusText || "Request failed",
    status,
    detail:
      typeof body === "string" && body.trim().length > 0 && body.length < 500 ? body : undefined,
  };
}
