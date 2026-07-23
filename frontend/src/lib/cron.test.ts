import { describe, it, expect } from "vitest";
import { parseCron, validateCron, describeCron, CRON_PRESETS } from "./cron";

/**
 * Tests for the client-side cron helpers. These pin the validation bounds and the
 * humanising output, including the crontab weekday convention (0 and 7 are both
 * Sunday) that the UI deliberately mirrors so the summary matches what the
 * operator typed. Fire-time computation is the backend's job and is not tested
 * here.
 */

describe("parseCron", () => {
  it("splits a valid 5-field expression", () => {
    expect(parseCron("0 1 * * *")).toEqual({
      minute: "0",
      hour: "1",
      dayOfMonth: "*",
      month: "*",
      dayOfWeek: "*",
    });
  });

  it("returns null for the wrong field count", () => {
    expect(parseCron("0 1 * *")).toBeNull();
    expect(parseCron("0 1 * * * *")).toBeNull();
  });

  it("tolerates extra whitespace between fields", () => {
    expect(parseCron("0   1 *  * *")).not.toBeNull();
  });
});

describe("validateCron", () => {
  it("accepts wildcards and in-range numbers", () => {
    expect(validateCron("0 1 * * *").valid).toBe(true);
    expect(validateCron("59 23 31 12 7").valid).toBe(true);
  });

  it("accepts steps, ranges and lists", () => {
    expect(validateCron("*/15 * * * *").valid).toBe(true);
    expect(validateCron("0 9-17 * * 1-5").valid).toBe(true);
    expect(validateCron("0 0,6,12,18 * * *").valid).toBe(true);
  });

  it("rejects an out-of-range field", () => {
    expect(validateCron("60 1 * * *").valid).toBe(false);
    expect(validateCron("0 24 * * *").valid).toBe(false);
    expect(validateCron("0 1 32 * *").valid).toBe(false);
    expect(validateCron("0 1 * 13 *").valid).toBe(false);
    expect(validateCron("0 1 * * 8").valid).toBe(false);
  });

  it("rejects an inverted range and a zero step", () => {
    expect(validateCron("0 17-9 * * *").valid).toBe(false);
    expect(validateCron("*/0 * * * *").valid).toBe(false);
  });

  it("reports a helpful error for the wrong field count", () => {
    expect(validateCron("0 1 * *").error).toMatch(/5 fields/);
  });
});

describe("describeCron", () => {
  it("describes a daily time", () => {
    expect(describeCron("0 1 * * *")).toBe("Every day at 01:00");
  });

  it("describes a weekly schedule using the crontab weekday", () => {
    // 0 is Sunday in crontab; the summary must say Sunday, not Monday.
    expect(describeCron("0 2 * * 0")).toBe("Every Sunday at 02:00");
    // 7 is also Sunday.
    expect(describeCron("0 2 * * 7")).toBe("Every Sunday at 02:00");
    expect(describeCron("30 3 * * 1")).toBe("Every Monday at 03:30");
  });

  it("describes an interval minute schedule", () => {
    expect(describeCron("*/15 * * * *")).toBe("Every 15 minutes");
  });

  it("describes a day-of-month schedule", () => {
    expect(describeCron("0 4 1 * *")).toBe("On day 1 of the month at 04:00");
  });

  it("returns an invalid marker for a malformed expression", () => {
    expect(describeCron("nonsense")).toBe("Invalid schedule");
    expect(describeCron("99 1 * * *")).toBe("Invalid schedule");
  });
});

describe("CRON_PRESETS", () => {
  it("only offers valid expressions", () => {
    for (const preset of CRON_PRESETS) {
      expect(validateCron(preset.expression).valid).toBe(true);
    }
  });
});
