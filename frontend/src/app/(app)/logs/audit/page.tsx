"use client";

import { useState } from "react";
import { AuthGuard } from "@/components/layout/auth-guard";
import { useAudit } from "@/hooks/queries";
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
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { RelativeTime } from "@/components/ui/primitives";
import type { AuditEventResponse } from "@/types/api";

/**
 * Security audit trail (UI.md page 8, admin-only). Every audited action with its
 * result, the acting user and IP, and the correlation id. Wrapped in an AuthGuard
 * requiring the admin role so a viewer who deep-links here is refused client-side
 * as well as by the API. A failed action is badged in danger so a security event
 * stands out at a glance.
 */
export default function AuditPage() {
  const [action, setAction] = useState("");
  const [result, setResult] = useState("");
  const [search, setSearch] = useState("");

  const query = useAudit({
    action: action || undefined,
    result: result || undefined,
    search: search || undefined,
  });

  return (
    <AuthGuard requiredRole="admin">
      <div>
        <PageHeader title="Audit trail" description="Security-relevant actions" />

        <div className="mb-4 flex flex-wrap items-center gap-2">
          <Input
            placeholder="Search action or user…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="max-w-xs"
          />
          <Select value={result} onChange={(e) => setResult(e.target.value)} aria-label="Result">
            <option value="">All results</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </Select>
          <Input
            placeholder="Action"
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="max-w-50"
          />
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
          emptyTitle="No audit events"
          emptyDescription="No audited action matches these filters."
        >
          {(d) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Action</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>User</TableHead>
                  <TableHead>IP</TableHead>
                  <TableHead>When</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.items.map((e: AuditEventResponse) => (
                  <TableRow key={e.id}>
                    <TableCell className="font-medium">{e.action}</TableCell>
                    <TableCell>
                      {e.result === "success" ? (
                        <Badge variant="success">success</Badge>
                      ) : (
                        <Badge variant="danger">failure</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-fg-muted">
                      {e.username ?? "\u2014"}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-fg-muted">
                      {e.ip_address ?? "\u2014"}
                    </TableCell>
                    <TableCell>
                      <RelativeTime iso={e.ts} className="text-xs text-fg-muted" />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </DataState>
      </div>
    </AuthGuard>
  );
}
