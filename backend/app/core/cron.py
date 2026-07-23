"""Cron expression validation and fire-time preview.

APScheduler's `CronTrigger` does the scheduling, so the expression the UI previews and the
expression the scheduler fires on are interpreted by the same code. A preview that disagreed
with the scheduler would be worse than no preview at all.

**`CronTrigger.from_crontab` is not used, because it is not crontab.** It splits the five
fields and passes them straight through, and APScheduler numbers weekdays from Monday while
crontab numbers them from Sunday. The schedule this project specifies — `0 1 * * 0`, "every
Sunday at 01:00" — therefore fires on *Monday* under `from_crontab`. Silently backing up on
the wrong day is exactly the kind of defect a backup tool cannot afford, so the day-of-week
field is translated to APScheduler's unambiguous weekday **names** before the trigger is
built.

Validation happens at the API edge, not when the scheduler starts: a cron typo must be a 400
on the request that introduced it, not a job that silently never fires.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from app.core.errors import ValidationFailed

CRON_FIELDS = 5
DEFAULT_CRON = "0 1 * * 0"
"""Weekly, Sunday 01:00 — the schedule named in the project brief."""

DAYS_PER_WEEK = 7
SUNDAY_ALIAS = 7
"""crontab accepts both 0 and 7 for Sunday."""

# Index = crontab's weekday number. APScheduler's own numbering starts on Monday, which is
# why translation goes through names rather than through arithmetic on the numbers.
_CRONTAB_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_APSCHEDULER_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_TO_CRONTAB = {name: index for index, name in enumerate(_CRONTAB_WEEKDAYS)}


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed(f"'{name}' is not a known IANA timezone") from exc


def _weekday_value(token: str) -> int:
    """One crontab weekday, as a crontab-numbered integer (0 = Sunday)."""
    cleaned = token.strip().lower()
    if cleaned.isdigit():
        value = int(cleaned)
        if value > SUNDAY_ALIAS:
            raise ValueError(f"'{token}' is not a weekday; expected 0-7 or a name like 'sun'")
        return 0 if value == SUNDAY_ALIAS else value
    if cleaned in _WEEKDAY_TO_CRONTAB:
        return _WEEKDAY_TO_CRONTAB[cleaned]
    raise ValueError(f"'{token}' is not a weekday; expected 0-7 or a name like 'sun'")


def _weekday_token(token: str) -> set[int]:
    base, separator, step_text = token.strip().partition("/")
    step = 1
    if separator:
        if not step_text.isdigit() or int(step_text) < 1:
            raise ValueError(f"'{token}' has an invalid step")
        step = int(step_text)

    base = base.strip()
    if base == "*":
        ordered = list(range(DAYS_PER_WEEK))
    elif "-" in base:
        start_text, _, end_text = base.partition("-")
        start, end = _weekday_value(start_text), _weekday_value(end_text)
        ordered = (
            list(range(start, end + 1))
            if start <= end
            # crontab allows a wrapping range such as fri-mon.
            else [*range(start, DAYS_PER_WEEK), *range(end + 1)]
        )
    else:
        first = _weekday_value(base)
        # In crontab, `n/step` means "from n to the end of the range, every step".
        ordered = list(range(first, DAYS_PER_WEEK)) if step > 1 else [first]

    return set(ordered[::step])


def translate_day_of_week(field: str) -> str:
    """Rewrite a crontab day-of-week field as APScheduler weekday names.

    Names remove the numbering question entirely: `sun` means Sunday to both systems.
    """
    if field.strip() == "*":
        return "*"

    days: set[int] = set()
    for part in field.split(","):
        if not part.strip():
            raise ValueError(f"'{field}' contains an empty day-of-week entry")
        days |= _weekday_token(part)

    if not days:  # pragma: no cover - every branch above yields at least one day
        raise ValueError(f"'{field}' selects no days")
    if len(days) == DAYS_PER_WEEK:
        return "*"
    return ",".join(name for name in _APSCHEDULER_ORDER if _WEEKDAY_TO_CRONTAB[name] in days)


def build_trigger(expression: str, timezone: str) -> CronTrigger:
    """Parse an expression, or raise `ValidationFailed` with a message naming the problem."""
    tzinfo = resolve_timezone(timezone)
    fields = expression.split()

    if len(fields) != CRON_FIELDS:
        raise ValidationFailed(
            f"'{expression}' is not a valid cron expression: expected {CRON_FIELDS} fields "
            f"(minute hour day-of-month month day-of-week), got {len(fields)}."
        )

    minute, hour, day, month, day_of_week = fields

    if day.strip() != "*" and day_of_week.strip() != "*":
        # Vixie cron ORs these two when both are restricted; APScheduler ANDs them. Rather
        # than quietly picking one, ProxSync refuses the expression — a schedule that means
        # something different from what the operator typed is worse than no schedule.
        raise ValidationFailed(
            f"'{expression}' restricts both day-of-month ('{day}') and day-of-week "
            f"('{day_of_week}'). Standard cron treats that as 'either', which ProxSync does "
            "not implement. Use one or the other, or create two schedules."
        )

    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=translate_day_of_week(day_of_week),
            timezone=tzinfo,
        )
    except ValueError as exc:
        raise ValidationFailed(f"'{expression}' is not a valid cron expression: {exc}") from exc


def validate_cron(expression: str, timezone: str) -> str:
    """Return the normalised expression, raising `ValidationFailed` if it cannot fire."""
    normalised = " ".join(expression.split())
    build_trigger(normalised, timezone)
    return normalised


def next_fire_times(
    expression: str, timezone: str, *, count: int = 5, after: datetime | None = None
) -> list[datetime]:
    """The next `count` fire times, in the job's own timezone.

    Returned in the job's timezone rather than UTC on purpose: an operator who wrote
    "01:00 Asia/Jakarta" wants to see 01:00, not 18:00 the previous day.
    """
    trigger = build_trigger(expression, timezone)
    tzinfo = resolve_timezone(timezone)
    reference = (after or datetime.now(tzinfo)).astimezone(tzinfo)

    fire_times: list[datetime] = []
    previous: datetime | None = None
    for _ in range(count):
        candidate: datetime | None = trigger.get_next_fire_time(previous, reference)
        if candidate is None:  # pragma: no cover - a 5-field crontab always recurs
            break
        fire_times.append(candidate)
        # Both arguments must advance. APScheduler computes from
        # `min(now, previous_fire_time)`, so leaving `now` at the original reference pins
        # every iteration to the same answer — a preview of five identical dates.
        previous = candidate
        reference = candidate
    return fire_times


def next_fire_time(
    expression: str, timezone: str, *, after: datetime | None = None
) -> datetime | None:
    times = next_fire_times(expression, timezone, count=1, after=after)
    return times[0] if times else None
