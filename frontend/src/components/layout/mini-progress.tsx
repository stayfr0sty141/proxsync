"use client";

import { useSyncExternalStore } from "react";
import { progressStore, type LiveProgress } from "@/lib/progress-store";
import { formatPercent, formatRate, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Live mini-progress pinned to the sidebar foot (UI.md shell).
 *
 * It subscribes to the progress store via useSyncExternalStore, so it re-renders
 * only when a `backup.progress`/`restore.progress` frame arrives — not on every
 * query invalidation. When no work is in flight the store snapshot is null and
 * this renders nothing, so the slot collapses rather than showing a stale 0%.
 * Collapsed (icon-rail) mode shows just the bar; expanded mode adds the label,
 * percentage and rate/ETA.
 */
export function MiniProgress({ collapsed }: { collapsed: boolean }) {
  const progress = useSyncExternalStore(
    progressStore.subscribe,
    progressStore.getSnapshot,
    () => null,
  );

  if (!progress) return null;

  const percent = progress.percent ?? 0;
  const label = describe(progress);

  return (
    <div className="flex flex-col gap-1" aria-live="polite">
      {!collapsed && (
        <div className="flex items-center justify-between text-[10px] text-fg-muted">
          <span className="truncate">{label}</span>
          <span className="font-mono">{formatPercent(percent)}</span>
        </div>
      )}
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-inset"
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div
          className={cn(
            "h-full rounded-full bg-accent transition-all",
            progress.percent === null && "animate-running-pulse w-1/3",
          )}
          style={progress.percent !== null ? { width: `${Math.min(100, percent)}%` } : undefined}
        />
      </div>
      {!collapsed && (
        <div className="flex items-center justify-between text-[10px] text-fg-subtle">
          <span>{sublabel(progress)}</span>
        </div>
      )}
    </div>
  );
}

function describe(progress: LiveProgress): string {
  if (progress.kind === "backup") {
    return progress.guestName
      ? `Backing up ${progress.guestName}`
      : `Backing up VMID ${progress.vmid}`;
  }
  return progress.phase ? `Restoring \u00b7 ${progress.phase}` : "Restoring";
}

function sublabel(progress: LiveProgress): string {
  if (progress.kind === "backup") {
    const rate = formatRate(progress.rateBps);
    const eta = progress.etaSeconds !== null ? `ETA ${formatDuration(progress.etaSeconds)}` : "";
    return [rate !== "\u2014" ? rate : "", eta].filter(Boolean).join(" \u00b7 ");
  }
  return "";
}
