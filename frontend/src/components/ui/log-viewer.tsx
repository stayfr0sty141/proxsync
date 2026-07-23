"use client";

import { formatAbsoluteTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { LogLevel } from "@/types/api";

/**
 * Monospace log viewer (UI.md LogViewer). Renders each entry on one dense line:
 * timestamp, a level glyph+colour (never colour alone), category, message, and a
 * clickable correlation id that pivots the filter to that trace. Kept as a simple
 * list rather than a virtualised one because the API already keyset-paginates to
 * a bounded page; the parent caps `limit`, so the DOM stays small.
 */

export interface LogViewerEntry {
  id: number;
  ts: string;
  level: LogLevel;
  category: string;
  message: string;
  correlationId: string | null;
}

const LEVEL_META: Record<LogLevel, { glyph: string; className: string }> = {
  debug: { glyph: "\u00b7", className: "text-fg-subtle" },
  info: { glyph: "i", className: "text-info" },
  warning: { glyph: "\u26a0", className: "text-warning" },
  error: { glyph: "\u2717", className: "text-danger" },
  critical: { glyph: "\u2691", className: "text-danger" },
};

export function LogViewer({
  entries,
  onCorrelationClick,
}: Readonly<{
  entries: LogViewerEntry[];
  onCorrelationClick?: (correlationId: string) => void;
}>) {
  return (
    <div className="overflow-hidden rounded-lg border border-border-default bg-inset">
      <div className="max-h-[70vh] overflow-y-auto font-mono text-xs">
        {entries.map((entry) => {
          const meta = LEVEL_META[entry.level];
          return (
            <div
              key={entry.id}
              className="flex items-start gap-2 border-b border-border-muted px-3 py-1.5 last:border-0 hover:bg-elevated"
            >
              <span className="shrink-0 text-fg-subtle" title={formatAbsoluteTime(entry.ts)}>
                {entry.ts.slice(11, 19)}
              </span>
              <span className={cn("w-4 shrink-0 text-center", meta.className)} aria-hidden="true">
                {meta.glyph}
              </span>
              <span className="w-20 shrink-0 truncate text-fg-muted">{entry.category}</span>
              <span className="min-w-0 flex-1 whitespace-pre-wrap wrap-break-word text-fg-default">
                {entry.message}
              </span>
              {entry.correlationId && (
                <button
                  type="button"
                  onClick={() => onCorrelationClick?.(entry.correlationId!)}
                  className="shrink-0 text-info hover:underline"
                  title="Filter by this correlation id"
                >
                  {entry.correlationId.slice(0, 8)}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
