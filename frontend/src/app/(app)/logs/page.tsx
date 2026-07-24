"use client";

import { useState } from "react";
import { useLogs } from "@/hooks/queries";
import { PageHeader } from "@/components/ui/page-header";
import { DataState } from "@/components/ui/data-state";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { LogViewer } from "@/components/ui/log-viewer";
import type { LogLevel } from "@/types/api";

/**
 * Log console (UI.md page 8). A filterable, monospace view of persisted logs with
 * category/level/free-text/correlation-id filters. The `persisted` and `dropped`
 * counts from the payload are surfaced as badges so an empty page is never
 * misread as a quiet night — a non-zero `dropped` means the bounded log buffer
 * shed its oldest entries under load, which is itself information (docs/HANDOFF).
 */
export default function LogsPage() {
  const [category, setCategory] = useState("");
  const [level, setLevel] = useState("");
  const [search, setSearch] = useState("");
  const [correlationId, setCorrelationId] = useState("");

  const params = {
    category: category || undefined,
    level: level || undefined,
    search: search || undefined,
    correlation_id: correlationId || undefined,
    limit: 200,
  };
  const query = useLogs(params);

  return (
    <div>
      <PageHeader
        title="Logs"
        description="Structured application logs"
        actions={
          query.data && (
            <div className="flex items-center gap-2">
              <Badge variant="info">{query.data.persisted} persisted</Badge>
              {query.data.dropped > 0 && (
                <Badge variant="warning">{query.data.dropped} dropped</Badge>
              )}
            </div>
          )
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search message…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
        />
        <Input
          placeholder="Correlation ID"
          value={correlationId}
          onChange={(e) => setCorrelationId(e.target.value)}
          className="max-w-50 font-mono"
        />
        <Select value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Level">
          <option value="">All levels</option>
          <option value="debug">Debug</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="error">Error</option>
          <option value="critical">Critical</option>
        </Select>
        <Select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          aria-label="Category"
        >
          <option value="">All categories</option>
          <option value="backup">Backup</option>
          <option value="restore">Restore</option>
          <option value="sync">Sync</option>
          <option value="retention">Retention</option>
          <option value="storage">Storage</option>
          <option value="auth">Auth</option>
          <option value="agent">Agent</option>
          <option value="scheduler">Scheduler</option>
          <option value="notify">Notify</option>
          <option value="system">System</option>
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
        emptyTitle="No log entries"
        emptyDescription="No log matches these filters."
        loadingRows={10}
      >
        {(d) => (
          <LogViewer
            entries={d.items.map((e) => ({
              id: e.id,
              ts: e.ts,
              level: e.level as LogLevel,
              category: e.category,
              message: e.message,
              correlationId: e.correlation_id,
            }))}
            onCorrelationClick={setCorrelationId}
          />
        )}
      </DataState>
    </div>
  );
}
