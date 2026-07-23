"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import { toast } from "sonner";
import { useBackupJob, useJobPreview, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/problem";
import { queryDomains } from "@/lib/query-keys";
import { PageHeader } from "@/components/ui/page-header";
import { DataState } from "@/components/ui/data-state";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { CronBuilder } from "@/components/dialogs/cron-builder";
import { RelativeTime } from "@/components/ui/primitives";
import type { BackupJobResponse } from "@/types/api";

/**
 * Schedule editor (UI.md page 4 detail). Edits a job's cron expression with the
 * live CronBuilder and shows the authoritative next-fire preview from the
 * backend. Enable/disable and run-now are one-click actions; each mutation
 * invalidates the jobs domain so the list and this page stay consistent, and the
 * SSE `job.state` event covers other open clients.
 */
export default function ScheduleEditorPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const job = useBackupJob(id);
  const preview = useJobPreview(id);

  return (
    <div>
      <PageHeader title="Edit schedule" description="Adjust when this backup job runs" />
      <DataState
        isLoading={job.isLoading}
        isError={job.isError}
        error={job.error}
        data={job.data}
        onRetry={() => {
          job.refetch();
        }}
      >
        {(data) => <ScheduleEditor job={data} previewTimes={preview.data?.next_fire_times ?? []} />}
      </DataState>
    </div>
  );
}

function ScheduleEditor({
  job,
  previewTimes,
}: Readonly<{
  job: BackupJobResponse;
  previewTimes: string[];
}>) {
  const [cron, setCron] = useState(job.cron_expression);

  const save = useInvalidatingMutation(
    (expression: string) =>
      api.put<BackupJobResponse>(`/backup-jobs/${job.id}`, { ...job, cron_expression: expression }),
    [queryDomains.jobs],
  );
  const toggle = useInvalidatingMutation(
    (enabled: boolean) => api.post(`/backup-jobs/${job.id}/${enabled ? "enable" : "disable"}`),
    [queryDomains.jobs],
  );
  const runNow = useInvalidatingMutation(
    () => api.post(`/backup-jobs/${job.id}/run`),
    [queryDomains.jobs, queryDomains.runs],
  );

  function handleSave() {
    save.mutate(cron, {
      onSuccess: () => toast.success("Schedule saved"),
      onError: (err) =>
        toast.error(
          err instanceof ApiError ? err.problem.detail || err.problem.title : "Save failed",
        ),
    });
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{job.name}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <CronBuilder value={cron} onChange={setCron} />
          <div className="flex flex-wrap gap-2">
            <Button onClick={handleSave} disabled={save.isPending || cron === job.cron_expression}>
              {save.isPending ? "Saving\u2026" : "Save"}
            </Button>
            <Button
              variant="secondary"
              onClick={() => toggle.mutate(!job.enabled)}
              disabled={toggle.isPending}
            >
              {job.enabled ? "Disable" : "Enable"}
            </Button>
            <Button
              variant="secondary"
              onClick={() =>
                runNow.mutate(undefined, {
                  onSuccess: () => toast.success("Backup queued"),
                  onError: () => toast.error("Could not queue backup"),
                })
              }
              disabled={runNow.isPending}
            >
              Run now
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Next runs</CardTitle>
        </CardHeader>
        <CardContent>
          {previewTimes.length === 0 ? (
            <p className="text-xs text-fg-muted">
              No upcoming runs — the job may be disabled or the schedule invalid.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {previewTimes.map((ts) => (
                <li key={ts} className="flex items-center justify-between text-sm">
                  <span className="font-mono text-xs text-fg-muted">
                    {ts.slice(0, 19).replace("T", " ")}
                  </span>
                  <RelativeTime iso={ts} className="text-xs text-fg-subtle" />
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
