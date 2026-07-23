import type { BackupProgressEvent, RestoreProgressEvent } from "@/types/events";

/**
 * A tiny in-memory pub/sub for high-frequency live progress.
 *
 * `backup.progress` and `restore.progress` arrive many times per second while a
 * transfer runs. Routing them through TanStack Query would either thrash the
 * cache or force a refetch storm, so instead the SSE hook pushes them here and
 * components (the mini-progress bar, a run detail row) subscribe with
 * `useSyncExternalStore`. A finished/absent transfer clears its slot so the bar
 * disappears rather than freezing at the last percentage.
 */

export interface LiveBackupProgress {
  kind: "backup";
  runId: number;
  vmid: number;
  guestName: string | null;
  percent: number | null;
  bytesDone: number | null;
  bytesTotal: number | null;
  rateBps: number | null;
  etaSeconds: number | null;
}

export interface LiveRestoreProgress {
  kind: "restore";
  restoreId: number;
  percent: number | null;
  phase: string | null;
}

export type LiveProgress = LiveBackupProgress | LiveRestoreProgress;

type Listener = () => void;

class ProgressStore {
  private backup: LiveBackupProgress | null = null;
  private restore: LiveRestoreProgress | null = null;
  private readonly listeners = new Set<Listener>();
  private snapshot: LiveProgress | null = null;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** Stable snapshot for useSyncExternalStore: restore takes visual priority. */
  getSnapshot = (): LiveProgress | null => {
    return this.snapshot;
  };

  pushBackup(event: BackupProgressEvent): void {
    this.backup = {
      kind: "backup",
      runId: event.run_id,
      vmid: event.vmid,
      guestName: event.guest_name,
      percent: event.percent,
      bytesDone: event.bytes_done,
      bytesTotal: event.bytes_total,
      rateBps: event.rate_bps,
      etaSeconds: event.eta_seconds,
    };
    this.recompute();
  }

  pushRestore(event: RestoreProgressEvent): void {
    this.restore = {
      kind: "restore",
      restoreId: event.restore_id,
      percent: event.percent,
      phase: event.phase,
    };
    this.recompute();
  }

  clearBackup(): void {
    if (this.backup) {
      this.backup = null;
      this.recompute();
    }
  }

  clearRestore(): void {
    if (this.restore) {
      this.restore = null;
      this.recompute();
    }
  }

  private recompute(): void {
    // A running restore is the more consequential operation, so it wins the
    // single mini-progress slot; otherwise show the backup if present.
    this.snapshot = this.restore ?? this.backup ?? null;
    for (const listener of this.listeners) listener();
  }
}

export const progressStore = new ProgressStore();
