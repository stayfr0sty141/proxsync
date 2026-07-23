"use client";

import { useBrowserCompare } from "@/hooks/queries";
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
import { Badge } from "@/components/ui/badge";
import { ByteSize } from "@/components/ui/primitives";
import type { ComparisonEntry } from "@/types/api";

/**
 * Backup browser (UI.md page 6). A reconciliation of what the local host holds
 * against what Google Drive holds, one row per artifact with a sync-state glyph
 * (= in sync, up = local only, down = remote only, != mismatch). The counts are
 * summarised as badges. When the remote cannot be listed the API returns a
 * `detail`, which DataState surfaces as a partial banner so an empty comparison
 * is never misread as "everything is in sync".
 */
export default function BrowserPage() {
  const query = useBrowserCompare();

  return (
    <div>
      <PageHeader
        title="Browser"
        description="Compare local artifacts against Google Drive"
        actions={
          query.data && (
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="success">{query.data.in_sync} in sync</Badge>
              <Badge variant="warning">{query.data.local_only} local only</Badge>
              <Badge variant="info">{query.data.remote_only} remote only</Badge>
              {query.data.size_mismatch > 0 && (
                <Badge variant="danger">{query.data.size_mismatch} mismatch</Badge>
              )}
            </div>
          )
        }
      />

      <DataState
        isLoading={query.isLoading}
        isError={query.isError}
        error={query.error}
        data={query.data}
        onRetry={() => {
          query.refetch();
        }}
        isEmpty={(d) => d.entries.length === 0}
        partialDetail={(d) => d.detail}
        emptyTitle="Nothing to compare"
        emptyDescription="No local or remote artifacts were found."
      >
        {(d) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>State</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Local size</TableHead>
                <TableHead>Remote size</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.entries.map((entry: ComparisonEntry) => (
                <TableRow key={entry.name}>
                  <TableCell>
                    <StatusBadge domain="syncState" status={entry.state} />
                  </TableCell>
                  <TableCell className="font-mono text-xs">{entry.name}</TableCell>
                  <TableCell>
                    <ByteSize bytes={entry.local_size} />
                  </TableCell>
                  <TableCell>
                    <ByteSize bytes={entry.remote_size} />
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
