"use client";

import { useParams } from "next/navigation";
import { toast } from "sonner";
import { useBackup, useBackupLog, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { PageHeader } from "@/components/ui/page-header";
import { DataState } from "@/components/ui/data-state";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { ByteSize, RelativeTime } from "@/components/ui/primitives";
import { formatDuration } from "@/lib/format";

/**
 * Backup detail (UI.md page 3 detail). Full metadata, the agent task log, and the
 * artifact actions (upload/verify/lock). Each action invalidates the backups
 * domain so the row and this page stay consistent; the SSE stream covers other
 * clients.
 */
export default function BackupDetailPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const backup = useBackup(id);
  const log = useBackupLog(id);

  const upload = useInvalidatingMutation(
    () => api.post(`/backups/${id}/upload`, { force: false }),
    [queryDomains.backups, queryDomains.sync],
  );
  const verify = useInvalidatingMutation(
    () => api.post(`/backups/${id}/verify`),
    [queryDomains.backups],
  );

  function runAction(action: "upload" | "verify") {
    const mutation = action === "upload" ? upload : verify;
    mutation.mutate(undefined, {
      onSuccess: () => toast.success(action === "upload" ? "Upload queued" : "Verification queued"),
      onError: (err) =>
        toast.error(
          err instanceof ApiError ? err.problem.detail || err.problem.title : "Action failed",
        ),
    });
  }

  return (
    <div>
      <PageHeader title="Backup detail" description="Artifact metadata, log and actions" />
      <DataState
        isLoading={backup.isLoading}
        isError={backup.isError}
        error={backup.error}
        data={backup.data}
        onRetry={() => {
          backup.refetch();
        }}
      >
        {(b) => (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>
                  {b.guest_name} ({b.vmid})
                </CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <DetailRow label="Status">
                  <StatusBadge domain="backup" status={b.status} />
                </DetailRow>
                <DetailRow label="Upload">
                  <StatusBadge domain="upload" status={b.upload_status} />
                </DetailRow>
                <DetailRow label="Size">
                  <ByteSize bytes={b.size_bytes} />
                </DetailRow>
                <DetailRow label="Duration">{formatDuration(b.duration_seconds)}</DetailRow>
                <DetailRow label="Finished">
                  <RelativeTime iso={b.finished_at} />
                </DetailRow>
                <DetailRow label="Filename">
                  <span className="font-mono text-xs">{b.filename}</span>
                </DetailRow>
                <DetailRow label="Checksum">
                  <span className="font-mono text-xs break-all">
                    {b.checksum_sha256 ?? "\u2014"}
                  </span>
                </DetailRow>

                <div className="flex flex-wrap gap-2 pt-2">
                  <Button size="sm" onClick={() => runAction("upload")} disabled={upload.isPending}>
                    Upload
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => runAction("verify")}
                    disabled={verify.isPending}
                  >
                    Verify
                  </Button>
                </div>
                {b.error_message && (
                  <p className="rounded-md border border-danger bg-danger-muted px-3 py-2 text-xs text-danger">
                    {b.error_message}
                  </p>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Task log</CardTitle>
              </CardHeader>
              <CardContent>
                <DataState
                  isLoading={log.isLoading}
                  isError={log.isError}
                  error={log.error}
                  data={log.data}
                  onRetry={() => {
                    log.refetch();
                  }}
                  isEmpty={(d) => d.lines.length === 0}
                  emptyTitle="No log"
                  emptyDescription="This backup produced no captured log output."
                  loadingRows={6}
                >
                  {(d) => (
                    <pre className="max-h-96 overflow-auto rounded-md bg-inset p-3 font-mono text-xs text-fg-muted">
                      {d.lines.join("\n")}
                    </pre>
                  )}
                </DataState>
              </CardContent>
            </Card>
          </div>
        )}
      </DataState>
    </div>
  );
}

function DetailRow({ label, children }: Readonly<{ label: string; children: React.ReactNode }>) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-border-muted pb-2 text-sm">
      <span className="text-fg-muted">{label}</span>
      <span className="text-right text-fg-default">{children}</span>
    </div>
  );
}
