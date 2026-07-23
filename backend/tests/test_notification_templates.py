"""Message rendering: formatting, escaping, and the caps that keep a message readable."""

from __future__ import annotations

import pytest

from app.schemas.enums import NotificationEvent
from app.services.notification_templates import (
    MAX_LISTED_GUESTS,
    guest_list,
    human_bytes,
    human_duration,
    human_percent,
    render,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "unknown"),
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (5 * 1024**3, "5.0 GiB"),
        (3 * 1024**4, "3.0 TiB"),
        (5000 * 1024**4, "5000.0 TiB"),
    ],
)
def test_byte_sizes_render_in_the_unit_an_operator_thinks_in(
    value: int | None, expected: str
) -> None:
    assert human_bytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "unknown"), (0, "0s"), (45, "45s"), (90, "1m 30s"), (3661, "1h 01m")],
)
def test_durations_render_without_leading_zero_units(value: float | None, expected: str) -> None:
    assert human_duration(value) == expected


def test_percentages_are_always_one_decimal() -> None:
    assert human_percent(87.99999) == "88.0%"
    assert human_percent("n/a") == "n/a"


def test_a_guest_list_names_the_first_few_and_counts_the_rest() -> None:
    """A message naming forty guests is not read."""
    guests = [
        {"guest_name": f"guest-{index}", "vmid": index, "guest_type": "vm"}
        for index in range(MAX_LISTED_GUESTS + 3)
    ]

    rendered = guest_list(guests)

    assert rendered.startswith("guest-0 (vm/0)")
    assert rendered.endswith("and 3 more")


def test_an_empty_guest_list_says_none_rather_than_nothing() -> None:
    assert guest_list([]) == "none"
    assert guest_list(None) == "none"


def test_every_event_has_a_template() -> None:
    """There is no fallback text: a new event with no message would otherwise ship as an
    empty Telegram send that fails at delivery time."""
    for event in NotificationEvent:
        assert render(event, {}).strip()


def test_interpolated_values_are_escaped() -> None:
    """A guest named `<b>prod` would otherwise break the message, or be silently swallowed by
    Telegram's HTML parser."""
    text = render(
        NotificationEvent.BACKUP_FAILED,
        {
            "run_id": 1,
            "guest_total": 1,
            "succeeded": 0,
            "failed_guests": [{"guest_name": "<b>prod</b>", "vmid": 101, "guest_type": "vm"}],
            "error": "vzdump exited 1 & died",
        },
    )

    assert "&lt;b&gt;prod&lt;/b&gt;" in text
    assert "exited 1 &amp; died" in text
    # The template's own markup survives.
    assert text.startswith("❌ <b>")


def test_a_partial_run_is_worded_differently_from_a_total_failure() -> None:
    common = {"run_id": 1, "guest_total": 3, "failed_guests": []}

    assert "Backup incomplete" in render(
        NotificationEvent.BACKUP_FAILED, {**common, "succeeded": 2}
    )
    assert "Backup failed" in render(NotificationEvent.BACKUP_FAILED, {**common, "succeeded": 0})


def test_an_interrupted_restore_does_not_claim_it_failed() -> None:
    """ "Failed" means nothing changed. "Interrupted" means nobody knows what changed, and an
    operator acting on the wrong one can overwrite a guest twice."""
    text = render(
        NotificationEvent.RESTORE_FAILED,
        {"restore_id": 4, "target_vmid": 155, "target_type": "vm", "status": "interrupted"},
    )

    assert "Restore outcome unknown" in text
    assert "interrupted" in text


def test_a_successful_restore_still_surfaces_a_warning() -> None:
    """A guest that would not start afterwards is a real outcome, not an error."""
    text = render(
        NotificationEvent.RESTORE_FINISHED,
        {
            "restore_id": 4,
            "target_vmid": 155,
            "target_type": "vm",
            "duration_seconds": 120,
            "warning": "the guest did not start",
        },
    )

    assert "⚠️ the guest did not start" in text


def test_an_overwriting_restore_says_so_up_front() -> None:
    text = render(
        NotificationEvent.RESTORE_STARTED,
        {
            "restore_id": 4,
            "filename": "vzdump-qemu-101.vma.zst",
            "target_vmid": 101,
            "target_type": "vm",
            "target_storage": "local-lvm",
            "source": "local",
            "overwrite": True,
        },
    )

    assert "overwriting the existing guest" in text


def test_the_storage_glyph_distinguishes_warning_from_critical() -> None:
    """Status is never colour-only, and a phone notification preview is mostly the first
    characters."""
    warning = render(NotificationEvent.STORAGE_THRESHOLD, {"severity": "warning"})
    critical = render(NotificationEvent.STORAGE_THRESHOLD, {"severity": "critical"})

    assert warning.startswith("🟠")
    assert critical.startswith("🔴")
