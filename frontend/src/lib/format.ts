/**
 * Pure formatting helpers shared across the UI.
 *
 * These are deliberately free of React and of any locale that would make a test
 * non-deterministic: byte and rate formatting use fixed IEC units, durations
 * render in a stable `1h 2m 3s` form, and relative time is computed against an
 * explicit `now` (defaulting to `Date.now()`) so tests can pin it. Every list
 * cell, progress readout and tooltip reads through here so a size or a duration
 * is rendered one way everywhere.
 */

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;

/**
 * Format a byte count as an IEC (base-1024) size, e.g. 1536 -> "1.5 KiB".
 * A null/undefined/negative input renders as an em dash so an unknown size is
 * visually distinct from a genuine zero.
 */
export function formatBytes(bytes: number | null | undefined, fractionDigits = 1): string {
  if (bytes === null || bytes === undefined || bytes < 0 || Number.isNaN(bytes)) {
    return "\u2014";
  }
  if (bytes === 0) return "0 B";

  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), BYTE_UNITS.length - 1);
  const value = bytes / Math.pow(1024, exponent);
  // Whole bytes never show a fraction; larger units respect fractionDigits.
  const digits = exponent === 0 ? 0 : fractionDigits;
  return `${value.toFixed(digits)} ${BYTE_UNITS[exponent]}`;
}

/** Format a transfer rate in bytes/sec as e.g. "12.0 MiB/s". */
export function formatRate(bytesPerSecond: number | null | undefined): string {
  if (
    bytesPerSecond === null ||
    bytesPerSecond === undefined ||
    bytesPerSecond < 0 ||
    Number.isNaN(bytesPerSecond)
  ) {
    return "\u2014";
  }
  return `${formatBytes(bytesPerSecond)}/s`;
}

/**
 * Render a duration in whole seconds as a compact `1h 2m 3s`. Sub-minute
 * durations keep seconds; multi-hour durations drop seconds to stay legible.
 */
export function formatDuration(totalSeconds: number | null | undefined): string {
  if (
    totalSeconds === null ||
    totalSeconds === undefined ||
    totalSeconds < 0 ||
    Number.isNaN(totalSeconds)
  ) {
    return "\u2014";
  }
  const seconds = Math.floor(totalSeconds);
  if (seconds === 0) return "0s";

  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;

  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  // Show seconds only when the whole duration is under an hour.
  if (secs > 0 && hours === 0) parts.push(`${secs}s`);
  return parts.join(" ") || `${seconds}s`;
}

/** Clamp a 0..100 (or 0..1) percentage to a stable one-decimal string. */
export function formatPercent(value: number | null | undefined, fractionDigits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "\u2014";
  }
  // Accept either a 0..1 fraction or a 0..100 percentage.
  const percent = value <= 1 && value > 0 ? value * 100 : value;
  const clamped = Math.max(0, Math.min(100, percent));
  return `${clamped.toFixed(fractionDigits)}%`;
}

const RELATIVE_UNITS: { limit: number; divisor: number; unit: string }[] = [
  { limit: 60, divisor: 1, unit: "s" },
  { limit: 3600, divisor: 60, unit: "m" },
  { limit: 86400, divisor: 3600, unit: "h" },
  { limit: 604800, divisor: 86400, unit: "d" },
  { limit: 2629800, divisor: 604800, unit: "w" },
  { limit: 31557600, divisor: 2629800, unit: "mo" },
];

/**
 * Format an ISO timestamp relative to `now`, e.g. "5m ago" / "in 2h". Returns an
 * em dash for null so an absent time never renders as "just now".
 */
export function formatRelativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) return "\u2014";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "\u2014";

  const deltaSeconds = (now - then) / 1000;
  const absSeconds = Math.abs(deltaSeconds);
  const past = deltaSeconds >= 0;

  if (absSeconds < 5) return "just now";

  for (const { limit, divisor, unit } of RELATIVE_UNITS) {
    if (absSeconds < limit) {
      const value = Math.floor(absSeconds / divisor);
      return past ? `${value}${unit} ago` : `in ${value}${unit}`;
    }
  }
  const years = Math.floor(absSeconds / 31557600);
  return past ? `${years}y ago` : `in ${years}y`;
}

/** Format an ISO timestamp as a stable absolute string for tooltips/detail. */
export function formatAbsoluteTime(iso: string | null | undefined): string {
  if (!iso) return "\u2014";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "\u2014";
  return date.toISOString().replace("T", " ").slice(0, 19) + " UTC";
}
