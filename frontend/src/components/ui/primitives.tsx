import { formatBytes, formatRelativeTime, formatAbsoluteTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Small presentational primitives shared across pages. Each keeps a single
 * formatting concern in one place so a byte size or a timestamp renders
 * identically everywhere (UI.md custom components ByteSize/RelativeTime).
 */

/** Render a byte count with the raw value exposed on hover for precision. */
export function ByteSize({
  bytes,
  className,
}: {
  bytes: number | null | undefined;
  className?: string;
}) {
  return (
    <span
      className={cn("tabular-nums", className)}
      title={bytes != null ? `${bytes} bytes` : undefined}
    >
      {formatBytes(bytes)}
    </span>
  );
}

/** Render a relative time with the absolute timestamp on hover. */
export function RelativeTime({
  iso,
  className,
}: {
  iso: string | null | undefined;
  className?: string;
}) {
  return (
    <time
      className={cn("whitespace-nowrap", className)}
      dateTime={iso ?? undefined}
      title={formatAbsoluteTime(iso)}
    >
      {formatRelativeTime(iso)}
    </time>
  );
}
