"""The capture side of log persistence.

Everything here is about what a log call must *never* do: block, raise, recurse, or grow
without bound.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.log_sink import LogSink
from app.schemas.enums import LogCategory, LogLevel


def event(**overrides: Any) -> dict[str, Any]:
    return {
        "event": "backup_finished",
        "level": "info",
        "timestamp": "2026-07-23T01:02:03Z",
        "logger": "proxsync.api",
        **overrides,
    }


def sink(**kwargs: Any) -> LogSink:
    instance = LogSink()
    instance.configure(
        capacity=kwargs.get("capacity", 10),
        level=kwargs.get("level", LogLevel.INFO),
        enabled=kwargs.get("enabled", True),
    )
    return instance


def capture(instance: LogSink, **overrides: Any) -> None:
    instance.capture(None, "info", event(**overrides))


# ---- capture -----------------------------------------------------------------


def test_an_event_becomes_a_row_shaped_entry() -> None:
    instance = sink()

    capture(instance, backup_id=812, vmid=101, correlation_id="abc123")

    entry = instance.drain(10)[0]
    assert entry.message == "backup_finished"
    assert entry.level is LogLevel.INFO
    assert entry.category is LogCategory.BACKUP
    assert entry.backup_id == 812
    assert entry.correlation_id == "abc123"
    assert entry.ts == datetime(2026, 7, 23, 1, 2, 3, tzinfo=UTC)


def test_the_processor_returns_the_event_dict_untouched() -> None:
    """It sits in the middle of the chain; swallowing or rewriting the dict would change what
    the console renders."""
    instance = sink()
    payload = event(vmid=101)

    assert instance.capture(None, "info", payload) is payload


def test_capture_is_off_until_it_is_configured_on() -> None:
    instance = LogSink()

    instance.capture(None, "info", event())

    assert instance.pending == 0


def test_entries_below_the_persist_level_are_not_stored() -> None:
    """The console can be chatty; the database is not the place for a per-poll debug line."""
    instance = sink(level=LogLevel.WARNING)

    capture(instance, level="info")
    capture(instance, level="error")

    entries = instance.drain(10)
    assert [entry.level for entry in entries] == [LogLevel.ERROR]


def test_an_explicit_category_wins_over_the_inferred_one() -> None:
    instance = sink()

    capture(instance, event="notification_sent", category="notify")

    assert instance.drain(1)[0].category is LogCategory.NOTIFY


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("backup_run_started", LogCategory.BACKUP),
        ("restore_download_finished", LogCategory.RESTORE),
        ("sync_transfer_failed", LogCategory.UPLOAD),
        ("retention_evaluated", LogCategory.RETENTION),
        ("schedule_missed", LogCategory.SCHEDULER),
        ("login_failed", LogCategory.AUTH),
        ("agent_request_failed", LogCategory.AGENT),
        ("notification_queued", LogCategory.NOTIFY),
        ("request_rejected", LogCategory.API),
        ("something_unclassified", LogCategory.SYSTEM),
    ],
)
def test_the_category_is_inferred_from_the_event_name(name: str, expected: LogCategory) -> None:
    instance = sink()

    capture(instance, event=name)

    assert instance.drain(1)[0].category is expected


def test_an_unknown_category_falls_back_rather_than_failing() -> None:
    """A bad `category=` must not cost the log line, and must not write a value the CHECK
    constraint would reject."""
    instance = sink()

    capture(instance, category="not-a-category")

    assert instance.drain(1)[0].category is LogCategory.BACKUP


def test_a_broken_timestamp_falls_back_to_now() -> None:
    instance = sink()

    capture(instance, timestamp="not-a-date")

    assert instance.drain(1)[0].ts.tzinfo is not None


def test_context_keeps_the_promoted_ids_as_well_as_the_columns() -> None:
    """The writer may have to null a foreign key to get a batch in; the id has to survive that
    somewhere readable."""
    instance = sink()

    capture(instance, backup_id=812)

    entry = instance.drain(1)[0]
    assert entry.backup_id == 812
    assert entry.context is not None
    assert entry.context["backup_id"] == 812


def test_context_drops_structlog_bookkeeping() -> None:
    instance = sink()

    capture(instance, vmid=101)

    context = instance.drain(1)[0].context or {}
    assert context == {"vmid": 101}


def test_unserialisable_values_are_stored_as_text_rather_than_lost() -> None:
    instance = sink()

    capture(instance, connection=object())

    context = instance.drain(1)[0].context or {}
    assert "object object at" in context["connection"]


def test_a_malformed_event_dict_never_raises() -> None:
    """A log call is not a place to introduce a new failure mode."""
    instance = sink()

    assert instance.capture(None, "info", {"event": object()}) is not None


# ---- bounds ------------------------------------------------------------------


def test_a_full_buffer_drops_the_oldest_and_counts_it() -> None:
    """Losing the start of an incident is bad; wedging a backup because logging is behind is
    worse."""
    instance = sink(capacity=3)

    for index in range(5):
        capture(instance, event=f"line_{index}")

    entries = instance.drain(10)
    assert [entry.message for entry in entries] == ["line_2", "line_3", "line_4"]
    assert instance.dropped == 2


def test_drain_takes_at_most_the_requested_batch() -> None:
    instance = sink()
    for index in range(5):
        capture(instance, event=f"line_{index}")

    first = instance.drain(2)

    assert [entry.message for entry in first] == ["line_0", "line_1"]
    assert instance.pending == 3


def test_a_restored_batch_goes_back_at_the_front_in_order() -> None:
    """A database hiccup must cost latency, not log lines."""
    instance = sink()
    for index in range(4):
        capture(instance, event=f"line_{index}")
    batch = instance.drain(2)

    instance.restore(batch)

    assert [entry.message for entry in instance.drain(4)] == [
        "line_0",
        "line_1",
        "line_2",
        "line_3",
    ]


def test_capture_is_suppressed_inside_paused() -> None:
    """Held by the writer while it talks to the database, so a failure to persist logs cannot
    generate the line describing it and refill the queue it just drained."""
    instance = sink()

    with instance.paused():
        capture(instance)
    capture(instance)

    assert instance.pending == 1


def test_reconfiguring_the_capacity_keeps_what_is_already_buffered() -> None:
    instance = sink(capacity=2)
    capture(instance, event="kept")

    instance.configure(capacity=50, level=LogLevel.INFO, enabled=True)

    assert [entry.message for entry in instance.drain(10)] == ["kept"]
