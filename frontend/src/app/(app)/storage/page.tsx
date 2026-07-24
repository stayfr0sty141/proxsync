"use client";

import {
  useStorageStatus,
  useStorageHistory,
  useStorageForecast,
  useStorageByGuest,
} from "@/hooks/queries";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UsageBar } from "@/components/ui/usage-bar";
import { DataState } from "@/components/ui/data-state";
import { StatusBadge } from "@/components/ui/status-badge";
import { ByteSize } from "@/components/ui/primitives";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StorageTrendChart } from "@/components/charts/storage-trend-chart";
import { formatDuration } from "@/lib/format";

/**
 * Storage monitor (UI.md page 7). Live local + remote usage, a 30-day trend
 * chart, the OLS forecast of days-until-full, and a per-guest consumption
 * breakdown. Each card is independently loaded so a slow forecast never blocks
 * the usage bars; the `storage.update` SSE event refreshes the whole domain.
 */
export default function StoragePage() {
  const status = useStorageStatus();
  const history = useStorageHistory(30);
  const forecast = useStorageForecast();
  const byGuest = useStorageByGuest();

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Storage" description="Local and Google Drive usage, trend and forecast" />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Usage</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <DataState
              isLoading={status.isLoading}
              isError={status.isError}
              error={status.error}
              data={status.data}
              onRetry={() => {
                status.refetch();
              }}
              loadingRows={2}
            >
              {(d) => (
                <div className="flex flex-col gap-4">
                  {d.local && (
                    <UsageBar
                      label="Local"
                      usedBytes={d.local.used_bytes}
                      totalBytes={d.local.total_bytes}
                    />
                  )}
                  <UsageBar
                    label="Google Drive"
                    usedBytes={d.remote.used_bytes}
                    totalBytes={d.remote.quota_bytes ?? d.remote.total_bytes}
                  />
                  {d.storages.map((pool) => (
                    <UsageBar
                      key={pool.name}
                      label={`${pool.name} (${pool.type})`}
                      usedBytes={pool.used_bytes}
                      totalBytes={pool.total_bytes}
                    />
                  ))}
                </div>
              )}
            </DataState>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Forecast</CardTitle>
          </CardHeader>
          <CardContent>
            <DataState
              isLoading={forecast.isLoading}
              isError={forecast.isError}
              error={forecast.error}
              data={forecast.data}
              onRetry={() => {
                forecast.refetch();
              }}
              loadingRows={2}
            >
              {(d) =>
                d.days_until_full != null ? (
                  <div className="flex flex-col gap-2">
                    <p className="text-2xl font-semibold text-fg">
                      {formatDuration(d.days_until_full * 86400)}
                    </p>
                    <p className="text-xs text-fg-muted">until local storage is full</p>
                    {d.growth_bytes_per_day != null && (
                      <p className="text-xs text-fg-subtle">
                        Growing ~<ByteSize bytes={d.growth_bytes_per_day} />
                        /day
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="text-xs text-fg-muted">{d.detail ?? "Not enough samples yet."}</p>
                )
              }
            </DataState>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>30-day trend</CardTitle>
        </CardHeader>
        <CardContent>
          <DataState
            isLoading={history.isLoading}
            isError={history.isError}
            error={history.error}
            data={history.data}
            onRetry={() => {
              history.refetch();
            }}
            isEmpty={(d) => d.items.length === 0}
            emptyTitle="No samples yet"
            emptyDescription="Storage is sampled every 15 minutes; the trend will populate shortly."
            loadingRows={4}
          >
            {(d) => <StorageTrendChart samples={d.items} />}
          </DataState>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Top consumers</CardTitle>
        </CardHeader>
        <CardContent>
          <DataState
            isLoading={byGuest.isLoading}
            isError={byGuest.isError}
            error={byGuest.error}
            data={byGuest.data}
            onRetry={() => {
              byGuest.refetch();
            }}
            isEmpty={(d) => d.items.length === 0}
            emptyTitle="No backups stored"
            emptyDescription="Per-guest consumption appears once backups exist."
          >
            {(d) => (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Guest</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Backups</TableHead>
                    <TableHead>Total size</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.items.map((g) => (
                    <TableRow key={g.vmid}>
                      <TableCell className="font-medium">
                        {g.guest_name} ({g.vmid})
                      </TableCell>
                      <TableCell>
                        <StatusBadge
                          domain="severity"
                          status="ok"
                          hideGlyph
                          className="uppercase"
                        />
                      </TableCell>
                      <TableCell>{g.backup_count}</TableCell>
                      <TableCell>
                        <ByteSize bytes={g.total_bytes} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </DataState>
        </CardContent>
      </Card>
    </div>
  );
}
