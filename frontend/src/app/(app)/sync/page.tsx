"use client";

import { toast } from "sonner";
import {
  useSyncStatus,
  useSyncTasks,
  useSyncQuota,
  useInvalidatingMutation,
} from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { queryDomains } from "@/lib/query-keys";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { UsageBar } from "@/components/ui/usage-bar";
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
import { Button } from "@/components/ui/button";
import { ByteSize, RelativeTime } from "@/components/ui/primitives";
import type { SyncTaskResponse } from "@/types/api";

/**
 * Sync queue and remote status (UI.md page 6). Queue depth as stat tiles, Drive
 * quota as a usage bar, and the transfer history with retry (for a failed one)
 * and cancel (for an in-flight one). The `sync.*` SSE events keep the queue and
 * task list live. Upload retries are safe (the object is overwritten), which is
 * why a failed transfer offers a one-click retry here.
 */
export default function SyncPage() {
  const status = useSyncStatus();
  const quota = useSyncQuota();
  const tasks = useSyncTasks();

  const retry = useInvalidatingMutation(
    (id: number) => api.post(`/sync/tasks/${id}/retry`),
    [queryDomains.sync],
  );
  const cancel = useInvalidatingMutation(
    (id: number) => api.post(`/sync/tasks/${id}/cancel`),
    [queryDomains.sync],
  );

  function handleRetry(id: number) {
    retry.mutate(id, {
      onSuccess: () => toast.success("Transfer requeued"),
      onError: () => toast.error("Could not retry"),
    });
  }

  function handleCancel(id: number) {
    cancel.mutate(id, {
      onSuccess: () => toast.success("Transfer cancelled"),
      onError: () => toast.error("Could not cancel"),
    });
  }

  const s = status.data;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Sync" description="Google Drive transfer queue and remote status" />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Queued" value={s?.queued ?? "\u2014"} loading={status.isLoading} />
        <StatCard
          label="Running"
          value={s?.running ?? "\u2014"}
          tone="accent"
          loading={status.isLoading}
        />
        <StatCard
          label="Failed"
          value={s?.failed ?? "\u2014"}
          tone="danger"
          loading={status.isLoading}
        />
        <StatCard
          label="Pending uploads"
          value={s?.pending_uploads ?? "\u2014"}
          loading={status.isLoading}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Google Drive quota</CardTitle>
        </CardHeader>
        <CardContent>
          <DataState
            isLoading={quota.isLoading}
            isError={quota.isError}
            error={quota.error}
            data={quota.data}
            onRetry={() => {
              quota.refetch();
            }}
            loadingRows={1}
          >
            {(q) => (
              <UsageBar label="Used of quota" usedBytes={q.used_bytes} totalBytes={q.total_bytes} />
            )}
          </DataState>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Transfers</CardTitle>
        </CardHeader>
        <CardContent>
          <DataState
            isLoading={tasks.isLoading}
            isError={tasks.isError}
            error={tasks.error}
            data={tasks.data}
            onRetry={() => {
              tasks.refetch();
            }}
            isEmpty={(d) => d.items.length === 0}
            emptyTitle="No transfers"
            emptyDescription="Uploads and downloads will appear here."
          >
            {(d) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Operation</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Size</TableHead>
                    <TableHead>Attempt</TableHead>
                    <TableHead>When</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.items.map((t: SyncTaskResponse) => {
                    const opName = t.operation || (t as unknown as Record<string, string>).direction || "upload";
                    const sizeBytes = t.bytes_total ?? t.bytes_done;

                    return (
                      <TableRow key={t.id}>
                        <TableCell>
                          <div className="flex flex-col">
                            <span className="capitalize font-medium text-fg-default">{opName}</span>
                            {t.filename && (
                              <span className="font-mono text-xs text-fg-muted truncate max-w-[280px]">
                                {t.filename}
                              </span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell>
                          <StatusBadge domain="sync" status={t.status} />
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-col">
                            <ByteSize bytes={sizeBytes} />
                            {t.percent !== null && t.percent !== undefined && (
                              <span className="text-xs text-brand font-mono font-medium">
                                {t.percent}%
                              </span>
                            )}
                          </div>
                        </TableCell>
                        <TableCell className="text-xs text-fg-muted">
                          {t.attempt}/{t.max_attempts}
                        </TableCell>
                        <TableCell>
                          <RelativeTime
                            iso={t.finished_at ?? t.created_at}
                            className="text-xs text-fg-muted"
                          />
                        </TableCell>
                      <TableCell>
                        {t.status === "failed" && (
                          <Button variant="ghost" size="sm" onClick={() => handleRetry(t.id)}>
                            Retry
                          </Button>
                        )}
                        {(t.status === "queued" || t.status === "running") && (
                          <Button variant="ghost" size="sm" onClick={() => handleCancel(t.id)}>
                            Cancel
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </DataState>
        </CardContent>
      </Card>
    </div>
  );
}
