/**
 * Client-side cron parsing and humanising for the schedule builder.
 *
 * This does NOT compute fire times — the backend owns that (docs/HANDOFF.md notes
 * the crontab-vs-APScheduler weekday bug fixed in M3). The UI only *describes* an
 * expression so an operator sees "Every day at 01:00" beside the raw field, and
 * *validates* shape client-side for instant feedback before the authoritative
 * `/backup-jobs/{id}/preview` round-trip. A 5-field crontab is assumed:
 *   minute hour day-of-month month day-of-week
 */

export interface CronParts {
  minute: string;
  hour: string;
  dayOfMonth: string;
  month: string;
  dayOfWeek: string;
}

export interface CronValidation {
  valid: boolean;
  error: string | null;
}

const FIELD_BOUNDS: { name: keyof CronParts; min: number; max: number }[] = [
  { name: "minute", min: 0, max: 59 },
  { name: "hour", min: 0, max: 23 },
  { name: "dayOfMonth", min: 1, max: 31 },
  { name: "month", min: 1, max: 12 },
  { name: "dayOfWeek", min: 0, max: 7 },
];

const WEEKDAY_NAMES = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
];

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/** Split a raw expression into named fields, or null if it is not 5 fields. */
export function parseCron(expression: string): CronParts | null {
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [minute, hour, dayOfMonth, month, dayOfWeek] = fields as [
    string,
    string,
    string,
    string,
    string,
  ];
  return { minute, hour, dayOfMonth, month, dayOfWeek };
}

function validateField(value: string, min: number, max: number): boolean {
  if (value === "*") return true;

  // Support a comma list of terms, each of which may be a step (*/n or a-b/n),
  // a range (a-b), or a single number.
  for (const term of value.split(",")) {
    const [rangePart, stepPart] = term.split("/") as [string, string | undefined];

    if (stepPart !== undefined) {
      const step = Number(stepPart);
      if (!Number.isInteger(step) || step <= 0) return false;
    }

    if (rangePart === "*") continue;

    if (rangePart.includes("-")) {
      const bounds = rangePart.split("-");
      if (bounds.length !== 2) return false;
      const lo = Number(bounds[0]);
      const hi = Number(bounds[1]);
      if (!Number.isInteger(lo) || !Number.isInteger(hi) || lo < min || hi > max || lo > hi) {
        return false;
      }
    } else {
      const num = Number(rangePart);
      if (!Number.isInteger(num) || num < min || num > max) return false;
    }
  }
  return true;
}

/** Validate a 5-field crontab expression for shape and per-field bounds. */
export function validateCron(expression: string): CronValidation {
  const parts = parseCron(expression);
  if (!parts) {
    return { valid: false, error: "A schedule needs exactly 5 fields." };
  }
  for (const { name, min, max } of FIELD_BOUNDS) {
    if (!validateField(parts[name], min, max)) {
      return { valid: false, error: `The ${name} field is out of range.` };
    }
  }
  return { valid: true, error: null };
}

function describeTime(minute: string, hour: string): string | null {
  if (minute === "*" && hour === "*") return "every minute";
  if (hour === "*" && /^\*\/\d+$/.test(minute)) {
    return `every ${minute.slice(2)} minutes`;
  }
  const m = Number(minute);
  const h = Number(hour);
  if (Number.isInteger(m) && Number.isInteger(h)) {
    const hh = String(h).padStart(2, "0");
    const mm = String(m).padStart(2, "0");
    return `at ${hh}:${mm}`;
  }
  return null;
}

/**
 * Produce a human summary of a cron expression, e.g. "Every day at 01:00" or
 * "Every Sunday at 02:30". Weekday numbers follow crontab convention (0 and 7 are
 * both Sunday) — the same convention the operator typed, deliberately *not*
 * APScheduler's, so what they read matches what they wrote.
 */
export function describeCron(expression: string): string {
  const parts = parseCron(expression);
  if (!parts) return "Invalid schedule";
  const { valid } = validateCron(expression);
  if (!valid) return "Invalid schedule";

  const time = describeTime(parts.minute, parts.hour);
  const timeText = time ?? "on a custom schedule";

  // Day-of-week takes precedence in the summary when constrained.
  if (parts.dayOfWeek !== "*") {
    if (/^\d$/.test(parts.dayOfWeek)) {
      const idx = Number(parts.dayOfWeek) % 7; // 7 -> 0 (Sunday)
      return `Every ${WEEKDAY_NAMES[idx]} ${timeText}`;
    }
    return `On selected weekdays ${timeText}`;
  }

  if (parts.dayOfMonth !== "*") {
    if (/^\d+$/.test(parts.dayOfMonth)) {
      return `On day ${parts.dayOfMonth} of the month ${timeText}`;
    }
    return `On selected days ${timeText}`;
  }

  if (parts.month !== "*" && /^\d+$/.test(parts.month)) {
    const idx = Number(parts.month) - 1;
    const name = MONTH_NAMES[idx] ?? parts.month;
    return `Every day in ${name} ${timeText}`;
  }

  // No day/month constraint: it runs daily.
  return time === "every minute" || time?.startsWith("every")
    ? capitalise(timeText)
    : `Every day ${timeText}`;
}

function capitalise(text: string): string {
  return text.length > 0 ? text[0]!.toUpperCase() + text.slice(1) : text;
}

/** Common presets offered by the cron builder as one-click starting points. */
export const CRON_PRESETS: { label: string; expression: string }[] = [
  { label: "Every day at 01:00", expression: "0 1 * * *" },
  { label: "Every Sunday at 02:00", expression: "0 2 * * 0" },
  { label: "Every 6 hours", expression: "0 */6 * * *" },
  { label: "Weekdays at 03:00", expression: "0 3 * * 1-5" },
  { label: "First of the month at 04:00", expression: "0 4 1 * *" },
];
