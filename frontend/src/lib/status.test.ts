import { describe, it, expect } from "vitest";
import { statusMeta } from "./status";

/**
 * Tests for the status metadata registry. The accessibility rule from UI.md —
 * status is never colour-only — is enforced here: every known status must carry a
 * non-empty glyph and a human label, and an unknown status must degrade to a
 * readable fallback rather than throwing.
 */

describe("statusMeta", () => {
  it("maps a known run status to a label, tone and glyph", () => {
    const meta = statusMeta("run", "success");
    expect(meta.label).toBe("Success");
    expect(meta.tone).toBe("success");
    expect(meta.glyph.length).toBeGreaterThan(0);
  });

  it("marks in-flight statuses as active so they can pulse", () => {
    expect(statusMeta("run", "running").active).toBe(true);
    expect(statusMeta("upload", "uploading").active).toBe(true);
    expect(statusMeta("restore", "running").active).toBe(true);
  });

  it("gives every known status in every domain a non-empty glyph", () => {
    const cases: [Parameters<typeof statusMeta>[0], string][] = [
      ["run", "failed"],
      ["backup", "interrupted"],
      ["upload", "hash_mismatch"],
      ["restore", "pending_confirmation"],
      ["sync", "cancelled"],
      ["notification", "suppressed"],
      ["syncState", "size_mismatch"],
      ["severity", "critical"],
    ];
    for (const [domain, status] of cases) {
      const meta = statusMeta(domain, status);
      expect(meta.glyph.length).toBeGreaterThan(0);
      expect(meta.label.length).toBeGreaterThan(0);
    }
  });

  it("falls back readably for an unknown status", () => {
    const meta = statusMeta("run", "some_new_state");
    expect(meta.tone).toBe("neutral");
    expect(meta.label).toBe("Some New State");
  });

  it("falls back for a null or undefined status without throwing", () => {
    expect(statusMeta("run", null).label).toBe("Unknown");
    expect(statusMeta("run", undefined).label).toBe("Unknown");
  });

  it("distinguishes tones across the upload lifecycle", () => {
    expect(statusMeta("upload", "verified").tone).toBe("success");
    expect(statusMeta("upload", "failed").tone).toBe("danger");
    expect(statusMeta("upload", "hash_unavailable").tone).toBe("warning");
  });
});
