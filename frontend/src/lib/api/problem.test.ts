import { describe, it, expect } from "vitest";
import { ApiError, NetworkError, toProblem } from "./problem";

/**
 * Tests for the error model. Every non-2xx response becomes an ApiError carrying
 * a parsed RFC 9457 problem; a transport failure becomes a NetworkError. These
 * pin the status classification the UI branches on and the graceful coercion of a
 * non-JSON body (an nginx/proxy 5xx) into a synthetic problem.
 */

describe("toProblem", () => {
  it("passes an RFC 9457 problem body through, trusting its status", () => {
    const problem = toProblem(
      400,
      { type: "about:blank", title: "Bad", status: 422, detail: "nope" },
      "Bad Request",
    );
    expect(problem.title).toBe("Bad");
    expect(problem.status).toBe(422);
    expect(problem.detail).toBe("nope");
  });

  it("falls back to the HTTP status when the body omits one", () => {
    const problem = toProblem(503, { title: "Down" }, "Service Unavailable");
    expect(problem.status).toBe(503);
  });

  it("synthesises a problem from a short plain-text body", () => {
    const problem = toProblem(502, "upstream boom", "Bad Gateway");
    expect(problem.title).toBe("Bad Gateway");
    expect(problem.detail).toBe("upstream boom");
    expect(problem.status).toBe(502);
  });

  it("omits an over-long text body from detail", () => {
    const long = "x".repeat(600);
    const problem = toProblem(500, long, "Internal Server Error");
    expect(problem.detail).toBeUndefined();
  });
});

describe("ApiError", () => {
  it("classifies common statuses", () => {
    expect(new ApiError(problem(401), null).isUnauthorized).toBe(true);
    expect(new ApiError(problem(403), null).isForbidden).toBe(true);
    expect(new ApiError(problem(409), null).isConflict).toBe(true);
    expect(new ApiError(problem(500), null).isUnauthorized).toBe(false);
  });

  it("exposes field errors when present", () => {
    const err = new ApiError({ ...problem(422), errors: { username: ["required"] } }, "corr-1");
    expect(err.fieldErrors.username).toEqual(["required"]);
    expect(err.correlationId).toBe("corr-1");
  });

  it("defaults field errors to an empty object", () => {
    expect(new ApiError(problem(400), null).fieldErrors).toEqual({});
  });

  it("uses detail then title for its message", () => {
    expect(new ApiError({ ...problem(400), detail: "d" }, null).message).toBe("d");
    expect(new ApiError({ type: "", title: "t", status: 400 }, null).message).toBe("t");
  });
});

describe("NetworkError", () => {
  it("carries a message and optional cause", () => {
    const cause = new Error("dns");
    const err = new NetworkError("unreachable", cause);
    expect(err.name).toBe("NetworkError");
    expect(err.message).toBe("unreachable");
    expect(err.cause).toBe(cause);
  });
});

function problem(status: number) {
  return { type: "about:blank", title: `Error ${status}`, status };
}
