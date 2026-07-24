"use client";

import { useMemo, useState } from "react";
import { toast } from "sonner";
import { useGuests, useStorageStatus, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  BackupMode,
  Compression,
  GuestResponse,
  ManualBackupRequest,
  RunAcceptedResponse,
} from "@/types/api";

/**
 * Enhanced Manual Backup Dialog.
 * Lists all VMs and LXCs with search, filtering (All / VM / LXC), and Select All / Deselect All.
 * Allows picking target local storage pool, mode, compression, and Google Drive upload toggle.
 */

const MODE_CONSEQUENCE: Record<BackupMode, string> = {
  snapshot: "No downtime; uses a storage snapshot where supported.",
  suspend: "Briefly suspends the guest for a consistent copy.",
  stop: "Stops the guest for the duration of the backup.",
};

export function ManualBackupDialog({ onClose }: Readonly<{ onClose: () => void }>) {
  const guests = useGuests(); // Fetches all guests (both VM and LXC)
  const storageStatus = useStorageStatus();

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [filterType, setFilterType] = useState<"all" | "vm" | "lxc">("all");
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<BackupMode>("snapshot");
  const [compression, setCompression] = useState<Compression>("zstd");
  const [storage, setStorage] = useState("local");
  const [upload, setUpload] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const run = useInvalidatingMutation(
    (payload: ManualBackupRequest) => api.post<RunAcceptedResponse>("/backups/run", payload),
    [queryDomains.runs, queryDomains.backups],
  );

  const storagePools = useMemo(
    () => storageStatus.data?.storages.filter((s) => s.active) ?? [],
    [storageStatus.data?.storages],
  );
  const allItems = useMemo(() => guests.data?.items ?? [], [guests.data?.items]);

  const filteredItems = useMemo(() => {
    return allItems.filter((g) => {
      const matchType = filterType === "all" || g.guest_type === filterType;
      const matchSearch =
        !search.trim() ||
        g.name.toLowerCase().includes(search.toLowerCase()) ||
        String(g.vmid).includes(search);
      return matchType && matchSearch;
    });
  }, [allItems, filterType, search]);

  const allFilteredSelected =
    filteredItems.length > 0 && filteredItems.every((g) => selected.has(g.vmid));

  function toggleAllFiltered() {
    if (allFilteredSelected) {
      setSelected((prev) => {
        const next = new Set(prev);
        filteredItems.forEach((g) => next.delete(g.vmid));
        return next;
      });
    } else {
      setSelected((prev) => {
        const next = new Set(prev);
        filteredItems.forEach((g) => next.add(g.vmid));
        return next;
      });
    }
  }

  function toggle(vmid: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(vmid)) next.delete(vmid);
      else next.add(vmid);
      return next;
    });
  }

  async function submit() {
    const selectedGuests = allItems.filter((g) => selected.has(g.vmid));
    if (selectedGuests.length === 0) {
      toast.error("Select at least one VM or LXC to backup");
      return;
    }

    setSubmitting(true);
    try {
      // Auto-enable backup permission for any selected disabled guests
      const disabledGuests = selectedGuests.filter((g) => !g.backup_enabled);
      if (disabledGuests.length > 0) {
        await Promise.all(
          disabledGuests.map((g) =>
            api.patch(`/guests/${g.vmid}?type=${g.guest_type}`, { backup_enabled: true }),
          ),
        );
        void guests.refetch();
      }

      const targets = selectedGuests.map((g) => ({ vmid: g.vmid, guest_type: g.guest_type }));

      await run.mutateAsync({ targets, mode, compression, storage, upload });
      toast.success(`Backup queued for ${targets.length} guest(s)`);
      onClose();
    } catch (err) {
      toast.error(
        err instanceof ApiError
          ? err.problem.detail || err.problem.title
          : "Could not queue backup",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Controls Header: Search + Type Filter + Select All */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Input
          placeholder="Search VM / LXC name or ID…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 max-w-xs text-xs"
        />

        <div className="flex items-center gap-2">
          <div className="flex rounded-md border border-border-muted bg-inset p-0.5 text-xs">
            <button
              type="button"
              className={cn(
                "rounded px-2.5 py-1 font-medium transition-colors",
                filterType === "all"
                  ? "bg-surface text-fg-default shadow-sm"
                  : "text-fg-muted hover:text-fg-default",
              )}
              onClick={() => setFilterType("all")}
            >
              All ({allItems.length})
            </button>
            <button
              type="button"
              className={cn(
                "rounded px-2.5 py-1 font-medium transition-colors",
                filterType === "vm"
                  ? "bg-surface text-fg-default shadow-sm"
                  : "text-fg-muted hover:text-fg-default",
              )}
              onClick={() => setFilterType("vm")}
            >
              VMs ({allItems.filter((g) => g.guest_type === "vm").length})
            </button>
            <button
              type="button"
              className={cn(
                "rounded px-2.5 py-1 font-medium transition-colors",
                filterType === "lxc"
                  ? "bg-surface text-fg-default shadow-sm"
                  : "text-fg-muted hover:text-fg-default",
              )}
              onClick={() => setFilterType("lxc")}
            >
              LXCs ({allItems.filter((g) => g.guest_type === "lxc").length})
            </button>
          </div>

          <Button type="button" variant="secondary" size="sm" onClick={toggleAllFiltered}>
            {allFilteredSelected ? "Deselect All" : "Select All"}
          </Button>
        </div>
      </div>

      {/* Guest Selection List */}
      <div className="max-h-64 overflow-y-auto rounded-md border border-border-muted bg-surface">
        {filteredItems.map((g: GuestResponse) => {
          const isChecked = selected.has(g.vmid);
          return (
            <label
              key={`${g.guest_type}-${g.vmid}`}
              aria-label={`Select ${g.name} (${g.vmid})`}
              className={cn(
                "flex cursor-pointer items-center gap-3 border-b border-border-muted px-3 py-2 text-xs last:border-0 hover:bg-elevated transition-colors",
                isChecked && "bg-elevated/60",
              )}
            >
              <input
                type="checkbox"
                checked={isChecked}
                onChange={() => toggle(g.vmid)}
                aria-label={`Select ${g.name} (${g.vmid})`}
                className="size-4 accent-accent"
              />
              <div className="flex flex-1 items-center gap-2">
                <span className="font-medium text-fg-default">{g.name}</span>
                <span className="text-fg-subtle">({g.vmid})</span>
              </div>
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase",
                    g.guest_type === "vm" ? "bg-accent/15 text-accent" : "bg-info/15 text-info",
                  )}
                >
                  {g.guest_type === "vm" ? "VM" : "LXC"}
                </span>
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[10px] capitalize",
                    g.status === "running"
                      ? "bg-success/15 text-success"
                      : "bg-fg-muted/15 text-fg-muted",
                  )}
                >
                  {g.status}
                </span>
              </div>
            </label>
          );
        })}
        {filteredItems.length === 0 && (
          <p className="px-3 py-6 text-center text-xs text-fg-muted">
            {guests.isLoading ? "Loading guests from Proxmox…" : "No guests match the search/filter."}
          </p>
        )}
      </div>

      {/* Backup Settings */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Target Storage</span>
          {storagePools.length > 0 ? (
            <Select value={storage} onChange={(e) => setStorage(e.target.value)}>
              {storagePools.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name} ({formatBytes(p.total_bytes - p.used_bytes)} free)
                </option>
              ))}
            </Select>
          ) : (
            <Input
              value={storage}
              onChange={(e) => setStorage(e.target.value)}
              placeholder="local"
            />
          )}
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Mode</span>
          <Select value={mode} onChange={(e) => setMode(e.target.value as BackupMode)}>
            <option value="snapshot">Snapshot (Recommended)</option>
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
            <option value="zstd">zstd (Fastest)</option>
            <option value="gzip">gzip</option>
            <option value="lzo">lzo</option>
            <option value="none">none</option>
          </Select>
        </div>
      </div>

      <label className="flex items-center gap-2 text-xs text-fg-default">
        <input
          type="checkbox"
          checked={upload}
          onChange={(e) => setUpload(e.target.checked)}
          className="size-4 accent-accent"
        />
        <span>Upload to Google Drive after backup</span>
      </label>

      {/* Actions */}
      <div className="flex items-center justify-between border-t border-border-muted pt-3">
        <span className="text-xs font-medium text-fg-muted">
          {selected.size} of {allItems.length} guest(s) selected
        </span>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={onClose} disabled={submitting || run.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={submitting || run.isPending || selected.size === 0}>
            {submitting || run.isPending ? "Queuing…" : "Start Backup Now"}
          </Button>
        </div>
      </div>
    </div>
  );
}
