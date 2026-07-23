"use client";

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
import { RelativeTime } from "@/components/ui/primitives";
import { describeCron } from "@/lib/cron";

/**
 * Schedules list (UI.md page 4). Each backup job shows its human-readable cron
 * summary, whether it is enabled, and when it next fires. Rows link into the job
 * editor. The list is kept live by the `job.state` SSE event.
 */
export default function SchedulesPage() {
  const query = useBackupJobs();

  return (
    <div>
      <PageHeader title="Schedules" description="Automated backup jobs and when they next run" />
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
    </div>
  );
}
