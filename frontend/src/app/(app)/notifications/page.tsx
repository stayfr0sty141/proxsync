"use client";

import { useState } from "react";
import { toast } from "sonner";
import { useNotifications, useInvalidatingMutation } from "@/hooks/queries";
import { api } from "@/lib/api/client";
import { queryDomains } from "@/lib/query-keys";
import { ApiError } from "@/lib/api/problem";
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
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { RelativeTime } from "@/components/ui/primitives";
import type { NotificationResponse, TelegramTestResponse } from "@/types/api";

/**
 * Notification outbox (UI.md page). Every queued/sent/failed/suppressed message
 * with its delivery state, a resend for a failed one, and a synchronous Telegram
 * test. The `notification.state` SSE event keeps delivery states live. A failed
 * send shows the real Telegram error verbatim (the backend passes it through),
 * and the test returns a 200 even when Telegram rejects, so we branch on `ok`.
 */
export default function NotificationsPage() {
  const [status, setStatus] = useState("");
  const query = useNotifications({ status: status || undefined });

  const resend = useInvalidatingMutation(
    (id: number) => api.post(`/notifications/${id}/resend`),
    [queryDomains.notifications],
  );
  const test = useInvalidatingMutation(
    () => api.post<TelegramTestResponse>("/notifications/telegram/test"),
    [queryDomains.notifications],
  );

  function handleTest() {
    test.mutate(undefined, {
      onSuccess: (res) =>
        res.ok
          ? toast.success("Test message delivered")
          : toast.error(res.description || res.detail || "Telegram rejected the message"),
      onError: (err) =>
        toast.error(
          err instanceof ApiError ? err.problem.detail || err.problem.title : "Test failed",
        ),
    });
  }

  function handleResend(id: number) {
    resend.mutate(id, {
      onSuccess: () => toast.success("Requeued"),
      onError: () => toast.error("Could not resend"),
    });
  }

  return (
    <div>
      <PageHeader
        title="Notifications"
        description="Telegram delivery outbox"
        actions={
          <Button variant="secondary" onClick={handleTest} disabled={test.isPending}>
            {test.isPending ? "Sending\u2026" : "Send test"}
          </Button>
        }
      />

      <div className="mb-4">
        <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="">All statuses</option>
          <option value="pending">Pending</option>
          <option value="sent">Sent</option>
          <option value="failed">Failed</option>
          <option value="suppressed">Suppressed</option>
        </Select>
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
        emptyTitle="No notifications"
        emptyDescription="Delivered and pending messages will appear here."
      >
        {(d) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Event</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Attempts</TableHead>
                <TableHead>When</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.items.map((n: NotificationResponse) => (
                <TableRow key={n.id}>
                  <TableCell>
                    <span className="font-medium">{n.event_type}</span>
                    {n.error_message && <p className="text-xs text-danger">{n.error_message}</p>}
                  </TableCell>
                  <TableCell>
                    <StatusBadge domain="notification" status={n.status} />
                  </TableCell>
                  <TableCell className="text-xs text-fg-muted">
                    {n.attempts}/{n.max_attempts}
                  </TableCell>
                  <TableCell>
                    <RelativeTime
                      iso={n.sent_at ?? n.created_at}
                      className="text-xs text-fg-muted"
                    />
                  </TableCell>
                  <TableCell>
                    {n.status === "failed" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleResend(n.id)}
                        disabled={resend.isPending}
                      >
                        Resend
                      </Button>
                    )}
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
