"use client";

import Link from "next/link";
import { Server, Box, Archive, CalendarClock } from "lucide-react";
import { useDashboardSummary, useDashboardActivity } from "@/hooks/queries";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { UsageBar } from "@/components/ui/usage-bar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { RelativeTime } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/data-state";

/**
 * Dashboard overview (UI.md page 2).
 *
 * Four stat tiles summarise the fleet and the backup cadence; the storage card
 * shows local and Drive usage; the activity feed lists the most recent backup,
 * restore and sync events. Everything reads from the two aggregate dashboard
 * endpoints and is kept live by the SSE stream (run.state / storage.update /
 * restore.state all invalidate the dashboard domain). Each card owns its own
 * loading skeleton so a slow endpoint never blanks the whole page.
 */
export default function DashboardPage() {
  const summary = useDashboardSummary();
  const activity = useDashboardActivity(15);
  const s = summary.data;

  return (
    <div>
      <PageHeader title="Dashboard" description="Fleet, backups and storage at a glance" />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Virtual machines"
          value={s ? `${s.guests.vm_running}/${s.guests.vm_total}` : "\u2014"}
          hint="running / total"
          icon={<Server className="size-4" />}
          loading={summary.isLoading}
        />
        <StatCard
          label="LXC containers"
          value={s ? `${s.guests.lxc_running}/${s.guests.lxc_total}` : "\u2014"}
          hint="running / total"
          icon={<Box className="size-4" />}
          loading={summary.isLoading}
        />
        <StatCard
          label="Last backup"
          value={
            s?.last_backup ? <StatusBadge domain="run" status={s.last_backup.status} /> : "\u2014"
          }
          hint={
            s?.last_backup ? <RelativeTime iso={s.last_backup.finished_at} /> : "No backups yet"
          }
          icon={<Archive className="size-4" />}
          loading={summary.isLoading}
        />
        <StatCard
          label="Next backup"
          value={s?.next_backup ? <RelativeTime iso={s.next_backup.next_run_at} /> : "\u2014"}
          hint={s?.next_backup ? s.next_backup.name : "No schedule enabled"}
          icon={<CalendarClock className="size-4" />}
          loading={summary.isLoading}
        />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Storage</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <UsageBar
              label="Local"
              usedBytes={s?.storage.local.used_bytes}
              totalBytes={s?.storage.local.total_bytes}
            />
            <UsageBar
              label="Google Drive"
              usedBytes={s?.storage.gdrive.used_bytes}
              totalBytes={s?.storage.gdrive.quota_bytes}
            />
            {s && (
              <div className="grid grid-cols-3 gap-2 pt-2 text-center">
                <JobStat label="Running" value={s.jobs.running} tone="info" />
                <JobStat label="Queued" value={s.jobs.queued} tone="neutral" />
                <JobStat label="Failed 24h" value={s.jobs.failed_24h} tone="danger" />
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent activity</CardTitle>
          </CardHeader>
          <CardContent>
            <DataState
              isLoading={activity.isLoading}
              isError={activity.isError}
              error={activity.error}
              data={activity.data}
              onRetry={() => void activity.refetch()}
              isEmpty={(d) => d.items.length === 0}
              emptyTitle="No activity yet"
              emptyDescription="Backups, restores and transfers will appear here as they run."
              loadingRows={6}
            >
              {(d) => (
                <ul className="flex flex-col divide-y divide-[var(--border-muted)]">
                  {d.items.map((item) => (
                    <li
                      key={`${item.kind}-${item.id}`}
                      className="flex items-center justify-between gap-3 py-2"
                    >
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate text-sm text-fg">{item.title}</span>
                        <span className="text-xs capitalize text-fg-subtle">{item.kind}</span>
                      </div>
                      <div className="flex shrink-0 items-center gap-3">
                        <StatusBadge domain={statusDomainFor(item.kind)} status={item.status} />
                        <RelativeTime iso={item.ts} className="text-xs text-fg-subtle" />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </DataState>
          </CardContent>
        </Card>
      </div>

      <div className="mt-4">
        <Link href="/backups" className="text-sm text-info hover:underline">
          {"View all backups \u2192"}
        </Link>
      </div>
    </div>
  );
}

function JobStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "info" | "neutral" | "danger";
}) {
  const toneClass = tone === "danger" ? "text-danger" : tone === "info" ? "text-info" : "text-fg";
  return (
    <div className="rounded-md bg-inset p-2">
      <p className={`text-lg font-semibold tabular-nums ${toneClass}`}>{value}</p>
      <p className="text-[10px] text-fg-muted">{label}</p>
    </div>
  );
}

function statusDomainFor(kind: string): "run" | "restore" | "sync" {
  if (kind === "restore") return "restore";
  if (kind === "sync") return "sync";
  return "run";
}
