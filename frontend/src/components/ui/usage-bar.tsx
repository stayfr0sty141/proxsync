import { formatBytes, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Horizontal usage bar for storage/quota (UI.md UsageBar).
 *
 * The fill colour crosses from accent to warning to danger as the used fraction
 * passes the same thresholds the backend uses (warning >= 80%, critical >= 90%),
 * but the percentage and the used/total figures are always shown as text so the
 * severity is never conveyed by colour alone. A null total renders an
 * indeterminate/unknown state rather than a misleading 0%.
 */
export function UsageBar({
  usedBytes,
  totalBytes,
  label,
  className,
}: {
  usedBytes: number | null | undefined;
  totalBytes: number | null | undefined;
  label?: string;
  className?: string;
}) {
  const known =
    usedBytes != null && totalBytes != null && totalBytes > 0 && !Number.isNaN(usedBytes);
  const percent = known ? Math.min(100, (usedBytes! / totalBytes!) * 100) : 0;

  const tone = percent >= 90 ? "bg-danger" : percent >= 80 ? "bg-warning" : "bg-accent";

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {label && (
        <div className="flex items-center justify-between text-xs">
          <span className="text-fg-muted">{label}</span>
          <span className="tabular-nums text-fg-subtle">
            {known ? (
              <>
                {formatBytes(usedBytes)} / {formatBytes(totalBytes)} ({formatPercent(percent)})
              </>
            ) : (
              "Unknown"
            )}
          </span>
        </div>
      )}
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-inset"
        role="progressbar"
        aria-valuenow={known ? Math.round(percent) : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label ?? "Usage"}
      >
        <div
          className={cn("h-full rounded-full transition-all", tone, !known && "opacity-30")}
          style={{ width: known ? `${percent}%` : "100%" }}
        />
      </div>
    </div>
  );
}
