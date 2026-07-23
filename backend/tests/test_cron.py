"""Cron validation and preview.

The specification names one schedule — weekly, Sunday 01:00 Asia/Jakarta, `0 1 * * 0` — so
that expression is asserted directly rather than only through a generic parser test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.cron import (
    DEFAULT_CRON,
    next_fire_time,
    next_fire_times,
    resolve_timezone,
    translate_day_of_week,
    validate_cron,
)
from app.core.errors import ValidationFailed

JAKARTA = ZoneInfo("Asia/Jakarta")


class TestDayOfWeekNumbering:
    """crontab numbers weekdays from Sunday; APScheduler numbers them from Monday.

    `CronTrigger.from_crontab` does not reconcile the two, so `0 1 * * 0` — the schedule this
    project specifies as "every Sunday" — fires on Monday if the field is passed through. The
    translation below is the fix, and these are the tests that pin it.
    """

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("0", "sun"),
            ("7", "sun"),
            ("1", "mon"),
            ("6", "sat"),
            ("*", "*"),
            ("0-6", "*"),
            ("1-5", "mon,tue,wed,thu,fri"),
            ("0,6", "sat,sun"),
            ("6,0", "sat,sun"),
            ("mon", "mon"),
            ("sun", "sun"),
            ("mon-fri", "mon,tue,wed,thu,fri"),
            # Wrapping range: Friday through Monday.
            ("fri-mon", "mon,fri,sat,sun"),
            # crontab counts from Sunday, so every second day is Sun, Tue, Thu, Sat.
            ("*/2", "tue,thu,sat,sun"),
        ],
    )
    def test_translation(self, field: str, expected: str) -> None:
        assert translate_day_of_week(field) == expected

    @pytest.mark.parametrize("field", ["8", "-1", "funday", "1-", ",", "1/0", "1//2"])
    def test_rejects_nonsense(self, field: str) -> None:
        with pytest.raises(ValueError, match=".*"):
            translate_day_of_week(field)

    def test_the_specified_weekly_schedule_actually_falls_on_sunday(self) -> None:
        after = datetime(2026, 7, 22, 12, 0, tzinfo=JAKARTA)  # a Wednesday
        fire = next_fire_times(DEFAULT_CRON, "Asia/Jakarta", count=1, after=after)[0]
        assert fire.strftime("%A") == "Sunday"
        assert fire.date().isoformat() == "2026-07-26"

    def test_weekday_names_and_numbers_agree(self) -> None:
        after = datetime(2026, 7, 22, 12, 0, tzinfo=JAKARTA)
        by_number = next_fire_times("0 1 * * 0", "Asia/Jakarta", count=3, after=after)
        by_name = next_fire_times("0 1 * * sun", "Asia/Jakarta", count=3, after=after)
        assert by_number == by_name


class TestAmbiguousExpressions:
    def test_day_of_month_and_day_of_week_together_are_refused(self) -> None:
        """Vixie cron ORs them, APScheduler ANDs them; guessing would be worse than refusing."""
        with pytest.raises(ValidationFailed) as excinfo:
            validate_cron("0 1 15 * 0", "UTC")
        assert "either" in excinfo.value.detail
        assert "two schedules" in excinfo.value.detail

    @pytest.mark.parametrize("expression", ["0 1 15 * *", "0 1 * * 0"])
    def test_one_or_the_other_is_fine(self, expression: str) -> None:
        assert validate_cron(expression, "UTC") == expression


class TestValidation:
    def test_accepts_the_default_schedule(self) -> None:
        assert validate_cron(DEFAULT_CRON, "Asia/Jakarta") == "0 1 * * 0"

    def test_normalises_whitespace(self) -> None:
        assert validate_cron("  0   1 *  * 0 ", "UTC") == "0 1 * * 0"

    @pytest.mark.parametrize(
        "expression",
        ["", "nonsense", "0 1 * *", "0 1 * * 0 7", "99 1 * * 0", "0 25 * * 0", "* * * * 9"],
    )
    def test_rejects_malformed_expressions(self, expression: str) -> None:
        with pytest.raises(ValidationFailed):
            validate_cron(expression, "UTC")

    def test_error_names_the_expression_and_the_expected_shape(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_cron("0 1 * *", "UTC")
        detail = excinfo.value.detail
        assert "0 1 * *" in detail
        assert "minute hour day-of-month month day-of-week" in detail

    def test_rejects_an_unknown_timezone(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            validate_cron("0 1 * * 0", "Mars/Olympus_Mons")
        assert "not a known IANA timezone" in excinfo.value.detail

    def test_resolve_timezone_returns_zoneinfo(self) -> None:
        assert resolve_timezone("Asia/Jakarta") == JAKARTA


class TestPreview:
    def test_weekly_sunday_at_one_in_jakarta(self) -> None:
        # A Wednesday.
        after = datetime(2026, 7, 22, 12, 0, tzinfo=JAKARTA)
        times = next_fire_times(DEFAULT_CRON, "Asia/Jakarta", count=3, after=after)

        assert len(times) == 3
        for fire in times:
            assert fire.weekday() == 6, "cron day-of-week 0 must mean Sunday"
            assert (fire.hour, fire.minute) == (1, 0)
        assert times[0].date().isoformat() == "2026-07-26"
        assert (times[1] - times[0]).days == 7

    def test_times_are_returned_in_the_jobs_timezone(self) -> None:
        """01:00 Jakarta is 18:00 UTC the day before, and an operator wants to see 01:00."""
        fire = next_fire_time(DEFAULT_CRON, "Asia/Jakarta")
        assert fire is not None
        assert fire.hour == 1
        assert fire.utcoffset() is not None
        assert fire.astimezone(UTC).hour == 18

    def test_strictly_increasing(self) -> None:
        times = next_fire_times("*/15 * * * *", "UTC", count=5)
        assert times == sorted(times)
        assert len(set(times)) == 5

    def test_next_fire_time_is_in_the_future(self) -> None:
        fire = next_fire_time(DEFAULT_CRON, "Asia/Jakarta")
        assert fire is not None
        assert fire > datetime.now(UTC)

    def test_dst_transition_does_not_duplicate_or_skip(self) -> None:
        """Europe/London crosses DST on 2026-03-29; a daily 02:30 job must still advance."""
        after = datetime(2026, 3, 27, 3, 0, tzinfo=ZoneInfo("Europe/London"))
        times = next_fire_times("30 2 * * *", "Europe/London", count=4, after=after)
        assert len(times) == 4
        assert len({fire.date() for fire in times}) == 4, "one firing per day, no duplicates"
