"use client";

import { useState } from "react";
import { toast } from "sonner";
import { useGuests, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type {
  BackupMode,
  Compression,
  GuestResponse,
  ManualBackupRequest,
  RunAcceptedResponse,
} from "@/types/api";

/**
 * Manual backup dialog (UI.md page 3). Picks guests, a mode with a plain-language
 * consequence, compression and storage, and whether to upload after. Submitting
 * posts to `/backups/run`; on success the run appears in history via the run.state
 * SSE event. Only backup-enabled guests are offered, since the backend enforces
 * the allow-list for manual backups too (docs/HANDOFF §M3).
 */

const MODE_CONSEQUENCE: Record<BackupMode, string> = {
  snapshot: "No downtime; uses a storage snapshot where supported.",
  suspend: "Briefly suspends the guest for a consistent copy.",
  stop: "Stops the guest for the duration of the backup.",
};

export function ManualBackupDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const guests = useGuests({ backup_enabled: true });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [mode, setMode] = useState<BackupMode>("snapshot");
  const [compression, setCompression] = useState<Compression>("zstd");
  const [storage] = useState("local");
  const [upload, setUpload] = useState(true);

  const run = useInvalidatingMutation(
    (payload: ManualBackupRequest) => api.post<RunAcceptedResponse>("/backups/run", payload),
    [queryDomains.runs, queryDomains.backups],
  );

  function toggle(vmid: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(vmid)) next.delete(vmid);
      else next.add(vmid);
      return next;
    });
  }

  function submit() {
    const items = guests.data?.items ?? [];
    const targets = items
      .filter((g) => selected.has(g.vmid))
      .map((g) => ({ vmid: g.vmid, guest_type: g.guest_type }));
    if (targets.length === 0) {
      toast.error("Select at least one guest");
      return;
    }
    run.mutate(
      { targets, mode, compression, storage, upload },
      {
        onSuccess: () => {
          toast.success("Backup queued");
          onClose();
        },
        onError: (err) =>
          toast.error(
            err instanceof ApiError
              ? err.problem.detail || err.problem.title
              : "Could not queue backup",
          ),
      },
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="max-h-64 overflow-y-auto rounded-md border border-border-muted">
        {(guests.data?.items ?? []).map((g: GuestResponse) => (
          <label
            key={g.vmid}
            className="flex cursor-pointer items-center gap-3 border-b border-border-muted px-3 py-2 last:border-0 hover:bg-elevated"
          >
            <input
              type="checkbox"
              checked={selected.has(g.vmid)}
              onChange={() => toggle(g.vmid)}
              className="size-4 accent-accent"
            />
            <span className="flex-1 text-sm text-fg-default">
              {g.name} <span className="text-fg-subtle">({g.vmid})</span>
            </span>
            <span className="text-xs uppercase text-fg-muted">{g.guest_type}</span>
          </label>
        ))}
        {guests.data?.items.length === 0 && (
          <p className="px-3 py-4 text-center text-xs text-fg-muted">
            No backup-enabled guests. Enable a guest first.
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Mode</span>
          <Select value={mode} onChange={(e) => setMode(e.target.value as BackupMode)}>
            <option value="snapshot">Snapshot</option>
            <option value="suspend">Suspend</option>
            <option value="stop">Stop</option>
          </Select>
          <span className="text-xs text-fg-subtle">{MODE_CONSEQUENCE[mode]}</span>
        </div>
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Compression</span>
          <Select
            value={compression}
            onChange={(e) => setCompression(e.target.value as Compression)}
          >
            <option value="zstd">zstd</option>
            <option value="gzip">gzip</option>
            <option value="lzo">lzo</option>
            <option value="none">none</option>
          </Select>
        </div>
      </div>

      <label className="flex items-center gap-2 text-sm text-fg-default">
        <input
          type="checkbox"
          checked={upload}
          onChange={(e) => setUpload(e.target.checked)}
          className="size-4 accent-accent"
        />
        <span>Upload to Google Drive after backup</span>
      </label>

      <div className="flex items-center justify-between">
        <span className="text-xs text-fg-muted">{selected.size} selected</span>
        <div className={cn("flex gap-2")}>
          <Button variant="secondary" onClick={onClose} disabled={run.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={run.isPending || selected.size === 0}>
            {run.isPending ? "Queuing\u2026" : "Start backup"}
          </Button>
        </div>
      </div>
    </div>
  );
}
