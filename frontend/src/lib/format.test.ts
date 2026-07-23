import { describe, it, expect } from "vitest";
import {
  formatBytes,
  formatRate,
  formatDuration,
  formatPercent,
  formatRelativeTime,
  formatAbsoluteTime,
} from "./format";

/**
 * Tests for the shared formatters. These are the most reused pure functions in
 * the UI, so their edge cases (null vs zero, unit boundaries, negative and NaN
 * inputs, past vs future relative time) are pinned here. The em dash sentinel is
 * \u2014.
 */

const DASH = "\u2014";

describe("formatBytes", () => {
  it("renders zero as an exact byte count, not a dash", () => {
    expect(formatBytes(0)).toBe("0 B");
  });

  it("renders whole bytes without a fraction", () => {
    expect(formatBytes(512)).toBe("512 B");
  });

  it("scales into IEC units at the 1024 boundary", () => {
    expect(formatBytes(1024)).toBe("1.0 KiB");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MiB");
    expect(formatBytes(1024 ** 3)).toBe("1.0 GiB");
  });

  it("honours the requested fraction digits above bytes", () => {
    expect(formatBytes(1536, 2)).toBe("1.50 KiB");
  });

  it("returns a dash for null, undefined, negative and NaN", () => {
    expect(formatBytes(null)).toBe(DASH);
    expect(formatBytes(undefined)).toBe(DASH);
    expect(formatBytes(-1)).toBe(DASH);
    expect(formatBytes(Number.NaN)).toBe(DASH);
  });

  it("distinguishes an unknown size (dash) from a real zero (0 B)", () => {
    expect(formatBytes(null)).not.toBe(formatBytes(0));
  });
});

describe("formatRate", () => {
  it("appends a per-second suffix", () => {
    expect(formatRate(1024)).toBe("1.0 KiB/s");
  });

  it("returns a dash for an unknown rate", () => {
    expect(formatRate(null)).toBe(DASH);
    expect(formatRate(-5)).toBe(DASH);
  });
});

describe("formatDuration", () => {
  it("renders zero seconds explicitly", () => {
    expect(formatDuration(0)).toBe("0s");
  });

  it("renders sub-minute durations in seconds", () => {
    expect(formatDuration(45)).toBe("45s");
  });

  it("combines minutes and seconds under an hour", () => {
    expect(formatDuration(125)).toBe("2m 5s");
  });

  it("drops seconds once an hour is reached", () => {
    expect(formatDuration(3661)).toBe("1h 1m");
    expect(formatDuration(7200)).toBe("2h");
  });

  it("returns a dash for null and negative input", () => {
    expect(formatDuration(null)).toBe(DASH);
    expect(formatDuration(-10)).toBe(DASH);
  });
});

describe("formatPercent", () => {
  it("treats a 0..1 fraction as a percentage", () => {
    expect(formatPercent(0.5)).toBe("50%");
  });

  it("passes a 0..100 value through", () => {
    expect(formatPercent(73)).toBe("73%");
  });

  it("clamps above 100", () => {
    expect(formatPercent(150)).toBe("100%");
  });

  it("respects fraction digits", () => {
    expect(formatPercent(12.345, 1)).toBe("12.3%");
  });

  it("returns a dash for null and NaN", () => {
    expect(formatPercent(null)).toBe(DASH);
    expect(formatPercent(Number.NaN)).toBe(DASH);
  });
});

describe("formatRelativeTime", () => {
  const now = Date.parse("2026-07-23T12:00:00Z");

  it("says 'just now' within five seconds", () => {
    expect(formatRelativeTime("2026-07-23T11:59:58Z", now)).toBe("just now");
  });

  it("renders a past time with an 'ago' suffix", () => {
    expect(formatRelativeTime("2026-07-23T11:55:00Z", now)).toBe("5m ago");
    expect(formatRelativeTime("2026-07-23T10:00:00Z", now)).toBe("2h ago");
  });

  it("renders a future time with an 'in' prefix", () => {
    expect(formatRelativeTime("2026-07-23T12:05:00Z", now)).toBe("in 5m");
  });

  it("returns a dash for null and an unparseable value", () => {
    expect(formatRelativeTime(null, now)).toBe(DASH);
    expect(formatRelativeTime("not-a-date", now)).toBe(DASH);
  });
});

describe("formatAbsoluteTime", () => {
  it("formats an ISO timestamp as a stable UTC string", () => {
    expect(formatAbsoluteTime("2026-07-23T12:34:56Z")).toBe("2026-07-23 12:34:56 UTC");
  });

  it("returns a dash for null", () => {
    expect(formatAbsoluteTime(null)).toBe(DASH);
  });
});
