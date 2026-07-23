"use client";

import { useRestores } from "@/hooks/queries";
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
import { RelativeTime } from "@/components/ui/primitives";
import { RestoreWizard } from "@/components/dialogs/restore-wizard";
import type { RestoreResponse } from "@/types/api";

/**
 * Restore page (UI.md page 5). The two-phase restore wizard sits at the top, and
 * the restore history below. Requires the operator role (enforced by the route
 * group and the API). The `restore.*` SSE events keep in-flight restores live.
 */
export default function RestorePage() {
  const history = useRestores();

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Restore" description="Restore a backup to a new or existing guest" />

      <RestoreWizard />

      <div>
        <h2 className="mb-3 text-sm font-semibold text-fg">Restore history</h2>
        <DataState
          isLoading={history.isLoading}
          isError={history.isError}
          error={history.error}
          data={history.data}
          onRetry={() => {
            history.refetch();
          }}
          isEmpty={(d) => d.items.length === 0}
          emptyTitle="No restores yet"
          emptyDescription="Completed and in-flight restores will appear here."
        >
          {(d) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Target VMID</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>When</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.items.map((r: RestoreResponse) => (
                  <TableRow key={r.id}>
                    <TableCell className="font-medium">{r.target_vmid}</TableCell>
                    <TableCell className="uppercase text-xs text-fg-muted">
                      {r.guest_type}
                    </TableCell>
                    <TableCell>
                      <StatusBadge domain="restore" status={r.status} />
                    </TableCell>
                    <TableCell className="capitalize text-xs text-fg-muted">{r.source}</TableCell>
                    <TableCell>
                      <RelativeTime
                        iso={r.finished_at ?? r.created_at}
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
    </div>
  );
}
