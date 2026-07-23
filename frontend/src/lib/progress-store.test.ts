import { describe, it, expect, beforeEach, vi } from "vitest";
import { progressStore } from "./progress-store";
import type { BackupProgressEvent, RestoreProgressEvent } from "@/types/events";

/**
 * Tests for the live-progress pub/sub. This store backs the mini-progress bar, so
 * its guarantees matter: a subscriber is notified on change, a running restore
 * takes visual priority over a backup, and clearing a slot removes it so the bar
 * disappears rather than freezing at the last percentage.
 */

const backupEvent: BackupProgressEvent = {
  run_id: 1,
  backup_id: 10,
  vmid: 101,
  guest_name: "web",
  percent: 42,
  bytes_done: 100,
  bytes_total: 200,
  rate_bps: 50,
  eta_seconds: 30,
};

const restoreEvent: RestoreProgressEvent = {
  restore_id: 5,
  percent: 60,
  phase: "extracting",
};

describe("progressStore", () => {
  beforeEach(() => {
    progressStore.clearBackup();
    progressStore.clearRestore();
  });

  it("starts with a null snapshot", () => {
    expect(progressStore.getSnapshot()).toBeNull();
  });

  it("exposes a pushed backup as the snapshot", () => {
    progressStore.pushBackup(backupEvent);
    const snap = progressStore.getSnapshot();
    expect(snap?.kind).toBe("backup");
    expect(snap?.percent).toBe(42);
  });

  it("prioritises a running restore over a backup", () => {
    progressStore.pushBackup(backupEvent);
    progressStore.pushRestore(restoreEvent);
    expect(progressStore.getSnapshot()?.kind).toBe("restore");
  });

  it("falls back to the backup when the restore is cleared", () => {
    progressStore.pushBackup(backupEvent);
    progressStore.pushRestore(restoreEvent);
    progressStore.clearRestore();
    expect(progressStore.getSnapshot()?.kind).toBe("backup");
  });

  it("returns to null when both slots are cleared", () => {
    progressStore.pushBackup(backupEvent);
    progressStore.clearBackup();
    expect(progressStore.getSnapshot()).toBeNull();
  });

  it("notifies subscribers on change and stops after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = progressStore.subscribe(listener);
    progressStore.pushBackup(backupEvent);
    expect(listener).toHaveBeenCalledTimes(1);
    unsubscribe();
    progressStore.pushRestore(restoreEvent);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("does not notify when clearing an already-empty slot", () => {
    const listener = vi.fn();
    progressStore.subscribe(listener);
    progressStore.clearBackup();
    expect(listener).not.toHaveBeenCalled();
  });
});
