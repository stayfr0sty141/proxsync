"use client";

import { useState } from "react";
import { toast } from "sonner";
import { useGuests, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { CronBuilder } from "@/components/dialogs/cron-builder";
import type {
  BackupJobCreate,
  BackupJobResponse,
  BackupMode,
  Compression,
  GuestResponse,
  TargetSelector,
} from "@/types/api";

/**
 * Dialog for creating a new backup schedule. Covers the full BackupJobCreate
 * payload: name, cron, timezone, mode, compression, storage, target selector
 * + guest list, retention and upload toggle.
 */
export function CreateScheduleDialog({
  onClose,
}: Readonly<{ onClose: () => void }>) {
  const guests = useGuests();

  // --- form state ---
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 1 * * 0");
  const [timezone] = useState("Asia/Jakarta");
  const [mode, setMode] = useState<BackupMode>("snapshot");
  const [compression, setCompression] = useState<Compression>("zstd");
  const [storage, setStorage] = useState("local");
  const [selector, setSelector] = useState<TargetSelector>("all");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [keepLocal, setKeepLocal] = useState(2);
  const [keepRemote, setKeepRemote] = useState(2);
  const [upload, setUpload] = useState(true);
  const [enabled, setEnabled] = useState(true);

  const create = useInvalidatingMutation(
    (payload: BackupJobCreate) =>
      api.post<BackupJobResponse>("/backup-jobs", payload),
    [queryDomains.jobs],
  );

  function toggleGuest(vmid: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(vmid)) next.delete(vmid);
      else next.add(vmid);
      return next;
    });
  }

  function submit() {
    if (!name.trim()) {
      toast.error("Schedule name is required");
      return;
    }
    if (selector !== "all" && selected.size === 0) {
      toast.error("Select at least one guest");
      return;
    }

    const items = guests.data?.items ?? [];
    const targets =
      selector === "all"
        ? []
        : items
            .filter((g) => selected.has(g.vmid))
            .map((g) => ({ vmid: g.vmid, guest_type: g.guest_type }));

    create.mutate(
      {
        name: name.trim(),
        cron_expression: cron,
        timezone,
        mode,
        compression,
        storage,
        target_selector: selector,
        targets,
        keep_local: keepLocal,
        keep_remote: keepRemote,
        upload_enabled: upload,
        enabled,
      },
      {
        onSuccess: () => {
          toast.success("Schedule created");
          onClose();
        },
        onError: (err) =>
          toast.error(
            err instanceof ApiError
              ? err.problem.detail || err.problem.title
              : "Could not create schedule",
          ),
      },
    );
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Name */}
      <div className="flex flex-col gap-1.5">
        <label htmlFor="sched-name" className="text-xs font-medium text-fg-muted">
          Name <span className="text-accent">*</span>
        </label>
        <Input
          id="sched-name"
          placeholder="e.g. Nightly all VMs"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      {/* Cron */}
      <div className="flex flex-col gap-1.5">
        <span className="text-xs font-medium text-fg-muted">Schedule</span>
        <CronBuilder value={cron} onChange={setCron} />
      </div>

      {/* Mode + Compression */}
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Mode</span>
          <Select
            value={mode}
            onChange={(e) => setMode(e.target.value as BackupMode)}
          >
            <option value="snapshot">Snapshot (no downtime)</option>
            <option value="suspend">Suspend</option>
            <option value="stop">Stop</option>
          </Select>
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

      {/* Storage */}
      <div className="flex flex-col gap-1.5">
        <span className="text-xs font-medium text-fg-muted">Storage</span>
        <Input
          placeholder="e.g. local, backup-hdd"
          value={storage}
          onChange={(e) => setStorage(e.target.value)}
        />
      </div>

      {/* Target selector */}
      <div className="flex flex-col gap-2">
        <span className="text-xs font-medium text-fg-muted">Targets</span>
        <div className="flex gap-2">
          {(["all", "include", "exclude"] as TargetSelector[]).map((s) => (
            <button
              key={s}
              onClick={() => setSelector(s)}
              className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                selector === s
                  ? "bg-accent text-white"
                  : "bg-elevated text-fg-muted hover:text-fg-default"
              }`}
            >
              {s === "all" ? "All guests" : s}
            </button>
          ))}
        </div>

        {selector !== "all" && (
          <div className="max-h-40 overflow-y-auto rounded-md border border-border-muted">
            {(guests.data?.items ?? []).map((g: GuestResponse) => (
              <label
                key={g.vmid}
                className="flex cursor-pointer items-center gap-3 border-b border-border-muted px-3 py-2 last:border-0 hover:bg-elevated"
              >
                <input
                  type="checkbox"
                  checked={selected.has(g.vmid)}
                  onChange={() => toggleGuest(g.vmid)}
                  className="size-4 accent-accent"
                />
                <span className="flex-1 text-sm text-fg-default">
                  {g.name}{" "}
                  <span className="text-fg-subtle">({g.vmid})</span>
                </span>
                <span className="text-xs uppercase text-fg-muted">
                  {g.guest_type}
                </span>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* Retention */}
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Keep local</span>
          <Input
            type="number"
            min={1}
            max={365}
            value={keepLocal}
            onChange={(e) => setKeepLocal(Number(e.target.value))}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-fg-muted">Keep remote</span>
          <Input
            type="number"
            min={0}
            max={365}
            value={keepRemote}
            onChange={(e) => setKeepRemote(Number(e.target.value))}
          />
        </div>
      </div>

      {/* Toggles */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <input
            id="sched-upload"
            type="checkbox"
            checked={upload}
            onChange={(e) => setUpload(e.target.checked)}
            className="size-4 accent-accent"
          />
          <label htmlFor="sched-upload" className="text-sm text-fg-default">
            Upload to Google Drive after backup
          </label>
        </div>
        <div className="flex items-center gap-2">
          <input
            id="sched-enabled"
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="size-4 accent-accent"
          />
          <label htmlFor="sched-enabled" className="text-sm text-fg-default">
            Enable schedule immediately
          </label>
        </div>
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-2 border-t border-border-muted pt-3">
        <Button variant="secondary" onClick={onClose} disabled={create.isPending}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create schedule"}
        </Button>
      </div>
    </div>
  );
}
