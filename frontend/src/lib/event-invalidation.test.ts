import { describe, it, expect, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import { invalidateForEvent, EVENT_INVALIDATIONS } from "./event-invalidation";
import { EVENT_NAMES } from "@/types/events";

/**
 * Tests for the SSE event -> query invalidation table. This is the seam that
 * turns a server event into a UI refresh, so its correctness matters: a state
 * event must invalidate its domain, and the two high-frequency progress events
 * must invalidate *nothing* (they drive the progress store instead, to avoid a
 * refetch storm while a transfer runs).
 */

function fakeClient() {
  const invalidateQueries = vi.fn();
  return { invalidateQueries } as unknown as QueryClient & {
    invalidateQueries: ReturnType<typeof vi.fn>;
  };
}

describe("invalidateForEvent", () => {
  it("invalidates the runs, backups and dashboard domains on run.state", () => {
    const client = fakeClient();
    invalidateForEvent(client, "run.state");
    const invalidated = client.invalidateQueries.mock.calls.map(
      (c) => (c[0] as { queryKey: string[] }).queryKey[0],
    );
    expect(invalidated).toContain("runs");
    expect(invalidated).toContain("backups");
    expect(invalidated).toContain("dashboard");
  });

  it("invalidates only the notifications domain on notification.state", () => {
    const client = fakeClient();
    invalidateForEvent(client, "notification.state");
    expect(client.invalidateQueries).toHaveBeenCalledTimes(1);
    expect((client.invalidateQueries.mock.calls[0]![0] as { queryKey: string[] }).queryKey[0]).toBe(
      "notifications",
    );
  });

  it("invalidates nothing for the high-frequency progress events", () => {
    const client = fakeClient();
    invalidateForEvent(client, "backup.progress");
    invalidateForEvent(client, "restore.progress");
    expect(client.invalidateQueries).not.toHaveBeenCalled();
  });

  it("invalidates nothing for stream.open", () => {
    const client = fakeClient();
    invalidateForEvent(client, "stream.open");
    expect(client.invalidateQueries).not.toHaveBeenCalled();
  });

  it("has an invalidation entry for every subscribed event name", () => {
    for (const name of EVENT_NAMES) {
      expect(EVENT_INVALIDATIONS[name]).toBeDefined();
    }
  });
});
