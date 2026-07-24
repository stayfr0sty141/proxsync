"use client";

import { useState } from "react";
import Link from "next/link";
import { useBackups } from "@/hooks/queries";
import { PageHeader } from "@/components/ui/page-header";
import { DataState } from "@/components/ui/data-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatusBadge } from "@/components/ui/status-badge";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { ByteSize, RelativeTime } from "@/components/ui/primitives";

/**
 * Backup history (UI.md page 3). A filterable table of every artifact: status and
 * upload state as accessible badges, size and finish time, and a link into the
 * per-backup detail. Filters live in local state and flow straight into the query
 * key, so each filter combination caches independently and the SSE `backup.state`
 * event keeps the visible page fresh without a manual refresh.
 */
import { Button } from "@/components/ui/button";
import { ManualBackupDialog } from "@/components/dialogs/manual-backup-dialog";

export default function BackupsPage() {
  const [status, setStatus] = useState("");
  const [uploadStatus, setUploadStatus] = useState("");
  const [guestType, setGuestType] = useState("");
  const [search, setSearch] = useState("");
  const [showBackupModal, setShowBackupModal] = useState(false);

  const params = {
    status: status || undefined,
    upload_status: uploadStatus || undefined,
    guest_type: guestType || undefined,
    search: search || undefined,
  };
  const query = useBackups(params);

  return (
    <div>
      <PageHeader
        title="Backups"
        description="Every backup artifact and its upload state"
        actions={
          <Button onClick={() => setShowBackupModal(true)}>
            ⚡ Run Manual Backup
          </Button>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search guest or filename…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
        />
        <Select
          value={guestType}
          onChange={(e) => setGuestType(e.target.value)}
          aria-label="Guest type"
        >
          <option value="">All types</option>
          <option value="vm">VM</option>
          <option value="lxc">LXC</option>
        </Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="">All statuses</option>
          <option value="success">Success</option>
          <option value="failed">Failed</option>
          <option value="running">Running</option>
          <option value="cancelled">Cancelled</option>
          <option value="interrupted">Interrupted</option>
        </Select>
        <Select
          value={uploadStatus}
          onChange={(e) => setUploadStatus(e.target.value)}
          aria-label="Upload status"
        >
          <option value="">All uploads</option>
          <option value="uploaded">Uploaded</option>
          <option value="verified">Verified</option>
          <option value="pending">Pending</option>
          <option value="failed">Upload failed</option>
          <option value="not_uploaded">Not uploaded</option>
        </Select>
      </div>

      <DataState
        isLoading={query.isLoading}
        isError={query.isError}
        error={query.error}
        data={query.data}
        onRetry={() => {
          query.refetch();
        }}
        isEmpty={(d) => d.items.length === 0}
        emptyTitle="No backups found"
        emptyDescription="No backup matches these filters yet."
        loadingRows={8}
      >
        {(d) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Guest</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Upload</TableHead>
                <TableHead>Size</TableHead>
                <TableHead>Finished</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.items.map((b) => (
                <TableRow key={b.id}>
                  <TableCell>
                    <Link
                      href={`/backups/${b.id}`}
                      className="font-medium text-info hover:underline"
                    >
                      {b.guest_name} ({b.vmid})
                    </Link>
                  </TableCell>
                  <TableCell className="uppercase text-xs text-fg-muted">{b.guest_type}</TableCell>
                  <TableCell>
                    <StatusBadge domain="backup" status={b.status} />
                  </TableCell>
                  <TableCell>
                    <StatusBadge domain="upload" status={b.upload_status} />
                  </TableCell>
                  <TableCell>
                    <ByteSize bytes={b.size_bytes} />
                  </TableCell>
                  <TableCell>
                    <RelativeTime iso={b.finished_at} className="text-xs text-fg-muted" />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </DataState>

      {showBackupModal && (
        <>
          <button
            type="button"
            aria-label="Close dialog"
            className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm"
            onClick={() => setShowBackupModal(false)}
          />
          <dialog
            open
            aria-labelledby="manual-backup-title"
            className="fixed left-1/2 top-1/2 z-50 m-0 w-full max-w-2xl -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border-muted bg-surface p-0 text-fg-default shadow-2xl"
            onKeyDown={(e) => {
              if (e.key === "Escape") setShowBackupModal(false);
            }}
          >
            <div className="flex items-center justify-between border-b border-border-muted px-5 py-4">
              <h2 id="manual-backup-title" className="text-base font-semibold">
                Start Manual Backup
              </h2>
              <button
                type="button"
                onClick={() => setShowBackupModal(false)}
                className="text-lg text-fg-muted transition-colors hover:text-fg-default"
                aria-label="Close dialog"
              >
                ✕
              </button>
            </div>
            <div className="max-h-[85vh] overflow-y-auto px-5 py-4">
              <ManualBackupDialog onClose={() => setShowBackupModal(false)} />
            </div>
          </dialog>
        </>
      )}
    </div>
  );
}
