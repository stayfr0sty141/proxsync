import { cn } from "@/lib/utils";
import { statusMeta, type StatusDomain, type StatusTone } from "@/lib/status";

/**
 * Accessible status badge.
 *
 * Per docs/UI.md status is never colour-only: every badge renders a text glyph
 * alongside the label, so it survives greyscale and a screen reader. Colours come
 * from the semantic `--color-*-muted` tokens, and a status whose meta is marked
 * `active` pulses (respecting prefers-reduced-motion, which is handled globally).
 */

const TONE_CLASSES: Record<StatusTone, string> = {
  success: "bg-success-muted text-success",
  warning: "bg-warning-muted text-warning",
  danger: "bg-danger-muted text-danger",
  info: "bg-info-muted text-info",
  accent: "bg-accent-muted text-accent",
  neutral: "bg-elevated text-fg-muted",
};

export interface StatusBadgeProps {
  domain: StatusDomain;
  status: string | null | undefined;
  className?: string;
  /** Hide the glyph (rare; only where space is extremely tight and a tooltip carries meaning). */
  hideGlyph?: boolean;
}

export function StatusBadge({ domain, status, className, hideGlyph }: StatusBadgeProps) {
  const meta = statusMeta(domain, status);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
        TONE_CLASSES[meta.tone],
        meta.active && "animate-running-pulse",
        className,
      )}
      // Expose the human label to assistive tech even if the glyph is decorative.
      role="status"
      aria-label={meta.label}
    >
      {!hideGlyph && (
        <span aria-hidden="true" className="font-mono leading-none">
          {meta.glyph}
        </span>
      )}
      <span>{meta.label}</span>
    </span>
  );
}
