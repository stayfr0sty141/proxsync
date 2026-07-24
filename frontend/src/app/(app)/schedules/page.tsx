"use client";

import { useState } from "react";
import Link from "next/link";
import { useBackupJobs } from "@/hooks/queries";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RelativeTime } from "@/components/ui/primitives";
import { describeCron } from "@/lib/cron";
import { CreateScheduleDialog } from "@/components/dialogs/create-schedule-dialog";

/**
 * Schedules list (UI.md page 4). Each backup job shows its human-readable cron
 * summary, whether it is enabled, and when it next fires. Rows link into the job
 * editor. The list is kept live by the `job.state` SSE event.
 * A "+ New Schedule" button opens the CreateScheduleDialog.
 */
export default function SchedulesPage() {
  const query = useBackupJobs();
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div>
      <div className="flex items-center justify-between">
        <PageHeader
          title="Schedules"
          description="Automated backup jobs and when they next run"
        />
        <Button onClick={() => setShowCreate(true)}>+ New Schedule</Button>
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
        emptyTitle="No schedules yet"
        emptyDescription="Create a backup job to run backups automatically on a cron schedule."
        emptyAction={
          <Button onClick={() => setShowCreate(true)}>+ New Schedule</Button>
        }
      >
        {(d) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Schedule</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Next run</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.items.map((job) => (
                <TableRow key={job.id}>
                  <TableCell>
                    <Link
                      href={`/schedules/${job.id}`}
                      className="font-medium text-info hover:underline"
                    >
                      {job.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-xs text-fg-muted">
                    {describeCron(job.cron_expression)}
                  </TableCell>
                  <TableCell>
                    {job.enabled ? (
                      <Badge variant="success">Enabled</Badge>
                    ) : (
                      <Badge variant="outline">Disabled</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <RelativeTime
                      iso={job.enabled ? job.next_run_at : null}
                      className="text-xs text-fg-muted"
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </DataState>

      {/* Create schedule modal */}
      {showCreate && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={(e) => {
            if (e.target === e.currentTarget) setShowCreate(false);
          }}
        >
          <div className="w-full max-w-lg rounded-xl border border-border-muted bg-surface shadow-2xl">
            <div className="flex items-center justify-between border-b border-border-muted px-5 py-4">
              <h2 className="text-base font-semibold text-fg-default">
                New Schedule
              </h2>
              <button
                onClick={() => setShowCreate(false)}
                className="text-lg text-fg-muted transition-colors hover:text-fg-default"
                aria-label="Close"
              >
                ✕
              </button>
            </div>
            <div className="max-h-[80vh] overflow-y-auto px-5 py-4">
              <CreateScheduleDialog onClose={() => setShowCreate(false)} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
