"use client";

import type { ReactNode } from "react";
import { ApiError, NetworkError } from "@/lib/api/problem";
import { Button } from "./button";
import { Skeleton } from "./skeleton";

/**
 * The four required list states (docs/UI.md): loading, empty, error, and partial.
 *
 * Centralising them means every page renders them identically — a skeleton while
 * loading, an explanatory empty state with an optional CTA, a typed error with a
 * retry, and a *partial* banner when the server returned stale/degraded data
 * (e.g. the agent was unreachable) so a zero count is never misread as "all
 * clear". Callers pass the query's flags and the resolved data.
 */

export interface DataStateProps<T> {
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  data: T | undefined;
  /** Treat this data as empty (e.g. `items.length === 0`). */
  isEmpty?: (data: T) => boolean;
  /** A degraded/partial signal from the payload (e.g. a `detail` on compare). */
  partialDetail?: (data: T) => string | null;
  onRetry?: () => void;
  loadingRows?: number;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
  children: (data: T) => ReactNode;
}

export function DataState<T>({
  isLoading,
  isError,
  error,
  data,
  isEmpty,
  partialDetail,
  onRetry,
  loadingRows = 5,
  emptyTitle = "Nothing here yet",
  emptyDescription = "There is no data to show.",
  emptyAction,
  children,
}: DataStateProps<T>) {
  if (isLoading) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true" aria-live="polite">
        {Array.from({ length: loadingRows }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    return <ErrorState error={error} onRetry={onRetry} />;
  }

  if (data === undefined) {
    return <ErrorState error={undefined} onRetry={onRetry} />;
  }

  if (isEmpty?.(data)) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border-default p-10 text-center">
        <p className="text-sm font-medium text-fg">{emptyTitle}</p>
        <p className="max-w-sm text-xs text-fg-muted">{emptyDescription}</p>
        {emptyAction && <div className="mt-2">{emptyAction}</div>}
      </div>
    );
  }

  const partial = partialDetail?.(data) ?? null;

  return (
    <div className="flex flex-col gap-3">
      {partial && (
        <div
          role="alert"
          className="flex items-center gap-2 rounded-md border border-warning bg-warning-muted px-3 py-2 text-xs text-warning"
        >
          <span aria-hidden="true">{"\u26A0"}</span>
          <span>{partial}</span>
        </div>
      )}
      {children(data)}
    </div>
  );
}

function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  let title = "Something went wrong";
  let detail = "An unexpected error occurred while loading this data.";

  if (error instanceof NetworkError) {
    title = "Can't reach the server";
    detail = "The request didn't get a response. Check your connection and retry.";
  } else if (error instanceof ApiError) {
    title = error.problem.title || title;
    detail = error.problem.detail || detail;
  }

  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-danger bg-danger-muted p-10 text-center"
    >
      <p className="text-sm font-medium text-danger">{title}</p>
      <p className="max-w-sm text-xs text-fg-muted">{detail}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" className="mt-2" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
