"""Enqueue policy: what gets said, what gets said again, and what gets withheld."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.telegram_client import TelegramMessage
from app.core.crypto import SecretBox
from app.core.errors import Conflict, TelegramError, ValidationFailed
from app.db.models.system import Notification
from app.repositories.notification_repository import SqlAlchemyNotificationRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import (
    NotificationChannel,
    NotificationEvent,
    NotificationStatus,
    SettingsSection,
)
from app.services.notification_service import NotificationService
from app.services.settings_service import SettingsService

from .conftest import SECRET_KEY

BOT_TOKEN = "999:test-bot-token"  # noqa: S105 - a fixture, not a credential
CHAT_ID = "-1009999"


class RecordingTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error: TelegramError | None = None

    async def send_message(
        self, *, token: str, chat_id: str, text: str, parse_mode: str | None = "HTML"
    ) -> TelegramMessage:
        self.sent.append(
            {"token": token, "chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        if self.error is not None:
            raise self.error
        return TelegramMessage(message_id=7, chat_id=chat_id, chat_title="ops")


@pytest.fixture
def telegram() -> RecordingTelegram:
    return RecordingTelegram()


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as value:
        yield value


async def configure(session: AsyncSession, **overrides: Any) -> None:
    """Seed defaults and switch Telegram on, as an admin would."""
    service = SettingsService(
        repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
    )
    await service.ensure_defaults()
    await service.update_section(
        SettingsSection.TELEGRAM,
        {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID, **overrides},
    )
    await session.commit()


def build(
    session: AsyncSession,
    *,
    telegram: RecordingTelegram | None = None,
    suppress_window_seconds: int = 300,
) -> NotificationService:
    return NotificationService(
        notifications=SqlAlchemyNotificationRepository(session),
        settings_service=SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        ),
        telegram=telegram,
        max_attempts=5,
        suppress_window_seconds=suppress_window_seconds,
    )


def storage_vars(severity: str = "warning") -> dict[str, Any]:
    return {
        "severity": severity,
        "previous_severity": "healthy",
        "used_bytes": 900,
        "total_bytes": 1000,
        "free_bytes": 100,
        "used_percent": 90,
    }


# ---- channel and per-event toggles -------------------------------------------


async def test_nothing_is_queued_while_the_channel_is_disabled(session: AsyncSession) -> None:
    await configure(session, enabled=False)

    record = await build(session).enqueue(
        NotificationEvent.STORAGE_THRESHOLD, variables=storage_vars()
    )

    assert record is None


async def test_a_disabled_event_writes_no_row(session: AsyncSession) -> None:
    """Off means not recorded at all: the outbox is a delivery log, and a row for something
    the operator asked never to hear about would be a queue that never drains."""
    await configure(session, notify_retention_deleted=False)
    service = build(session)

    assert (
        await service.enqueue(
            NotificationEvent.RETENTION_DELETED,
            variables={
                "vmid": 101,
                "guest_type": "vm",
                "guest_name": "web",
                "deleted_local": 1,
                "deleted_remote": 0,
                "freed_bytes": 10,
                "keep_local": 2,
                "keep_remote": 2,
            },
        )
        is None
    )
    assert await service.list() == await service.list()
    assert (await service.list()).total == 0


async def test_an_enabled_event_is_queued_with_its_rendered_text(session: AsyncSession) -> None:
    await configure(session)

    record = await build(session).enqueue(
        NotificationEvent.BACKUP_SUCCESS,
        variables={
            "run_id": 12,
            "guest_total": 3,
            "succeeded": 3,
            "duration_seconds": 90,
            "total_bytes": 2 * 1024**3,
        },
        dedupe_key="run:12:backup_success",
        unique=True,
    )

    assert record is not None
    assert record.status == NotificationStatus.PENDING.value
    assert record.channel == NotificationChannel.TELEGRAM.value
    assert record.dedupe_key == "run:12:backup_success"
    # Frozen at enqueue time, so an outage cannot make the message describe a later world.
    assert "Backup finished" in record.payload["text"]
    assert "2.0 GiB" in record.payload["text"]


# ---- de-duplication ----------------------------------------------------------


async def test_a_unique_key_is_written_exactly_once(session: AsyncSession) -> None:
    """This is what makes 'exactly once' survive a restart: the guarantee is a row, not a
    timer."""
    await configure(session)
    service = build(session)
    variables = {"run_id": 12, "guest_total": 1, "succeeded": 1, "duration_seconds": 5}

    first = await service.enqueue(
        NotificationEvent.BACKUP_SUCCESS,
        variables=variables,
        dedupe_key="run:12:backup_success",
        unique=True,
    )
    second = await service.enqueue(
        NotificationEvent.BACKUP_SUCCESS,
        variables=variables,
        dedupe_key="run:12:backup_success",
        unique=True,
    )

    assert first is not None
    assert second is None
    assert (await service.list()).total == 1


async def test_a_unique_key_is_still_honoured_after_the_window_elapses(
    session: AsyncSession,
) -> None:
    await configure(session)
    service = build(session, suppress_window_seconds=1)
    variables = {"restore_id": 4, "target_vmid": 155, "target_type": "vm", "status": "failed"}

    await service.enqueue(
        NotificationEvent.RESTORE_FAILED,
        variables=variables,
        dedupe_key="restore:4:restore_failed",
        unique=True,
    )
    await session.flush()
    # Age the first row well past the suppression window.
    stored = (await service.list()).items[0]
    record = await service.get(stored.id)
    record.created_at = datetime.now(UTC) - timedelta(hours=6)
    await session.flush()

    again = await service.enqueue(
        NotificationEvent.RESTORE_FAILED,
        variables=variables,
        dedupe_key="restore:4:restore_failed",
        unique=True,
    )

    assert again is None


async def test_a_repeat_inside_the_window_is_recorded_as_suppressed(
    session: AsyncSession,
) -> None:
    """Recorded, not dropped: "we decided not to tell you at 03:14" is itself information."""
    await configure(session)
    service = build(session)

    first = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )
    second = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )

    assert first is not None
    assert second is not None
    assert second.status == NotificationStatus.SUPPRESSED.value
    assert second.payload["suppressed_by"] == first.id
    assert "within the last 300s" in second.payload["suppressed_reason"]


async def test_a_different_severity_is_news_even_inside_the_window(
    session: AsyncSession,
) -> None:
    """A disk that keeps hovering on the warning line must not alert every sample, but going
    from warning to critical is a different fact."""
    await configure(session)
    service = build(session)

    await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )
    escalation = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars("critical"),
        dedupe_key="storage:critical",
    )

    assert escalation is not None
    assert escalation.status == NotificationStatus.PENDING.value


async def test_a_repeat_after_the_window_is_queued_again(session: AsyncSession) -> None:
    await configure(session)
    service = build(session, suppress_window_seconds=1)

    first = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )
    assert first is not None
    first.created_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.flush()

    second = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )

    assert second is not None
    assert second.status == NotificationStatus.PENDING.value


async def test_a_failed_predecessor_does_not_suppress_its_successor(
    session: AsyncSession,
) -> None:
    """Only queued or delivered messages suppress. A message that failed to send did not tell
    anyone anything, so the next one has to get through."""
    await configure(session)
    service = build(session)

    first = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )
    assert first is not None
    first.status = NotificationStatus.FAILED.value
    await session.flush()

    second = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )

    assert second is not None
    assert second.status == NotificationStatus.PENDING.value


# ---- listing and resend ------------------------------------------------------


async def test_listing_filters_and_counts_pending(session: AsyncSession) -> None:
    await configure(session)
    service = build(session)
    await service.enqueue(
        NotificationEvent.BACKUP_SUCCESS,
        variables={"run_id": 1, "guest_total": 1, "succeeded": 1},
        dedupe_key="run:1:backup_success",
        unique=True,
    )
    await service.enqueue(
        NotificationEvent.BACKUP_FAILED,
        variables={"run_id": 2, "guest_total": 1, "succeeded": 0, "failed_guests": []},
        dedupe_key="run:2:backup_failed",
        unique=True,
    )

    everything = await service.list()
    failures = await service.list(event=NotificationEvent.BACKUP_FAILED)

    assert everything.total == 2
    assert everything.pending == 2
    assert failures.total == 1
    assert failures.items[0].event_type is NotificationEvent.BACKUP_FAILED


async def test_resend_requeues_a_failed_message(session: AsyncSession) -> None:
    await configure(session)
    service = build(session)
    record = await service.enqueue(
        NotificationEvent.BACKUP_FAILED,
        variables={"run_id": 2, "guest_total": 1, "succeeded": 0, "failed_guests": []},
        dedupe_key="run:2:backup_failed",
        unique=True,
    )
    assert record is not None
    record.status = NotificationStatus.FAILED.value
    record.attempts = 5
    record.error_message = "chat not found"
    await session.flush()

    requeued = await service.resend(record.id)

    assert requeued.status == NotificationStatus.PENDING.value
    assert requeued.attempts == 0
    assert requeued.next_attempt_at is None
    assert requeued.error_message is None


async def test_resend_refuses_a_message_that_is_still_queued(session: AsyncSession) -> None:
    """Clearing its attempt count would restart a backoff that exists precisely because
    Telegram is refusing it right now."""
    await configure(session)
    service = build(session)
    record = await service.enqueue(
        NotificationEvent.BACKUP_FAILED,
        variables={"run_id": 3, "guest_total": 1, "succeeded": 0, "failed_guests": []},
        dedupe_key="run:3:backup_failed",
        unique=True,
    )
    assert record is not None

    with pytest.raises(Conflict):
        await service.resend(record.id)


async def test_a_suppressed_message_can_be_asked_for_anyway(session: AsyncSession) -> None:
    await configure(session)
    service = build(session)
    await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )
    suppressed = await service.enqueue(
        NotificationEvent.STORAGE_THRESHOLD,
        variables=storage_vars(),
        dedupe_key="storage:warning",
    )
    assert suppressed is not None

    requeued = await service.resend(suppressed.id)

    assert requeued.status == NotificationStatus.PENDING.value


# ---- the test message --------------------------------------------------------


async def test_send_test_delivers_immediately_and_records_the_outcome(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    await configure(session)
    service = build(session, telegram=telegram)

    result = await service.send_test(requested_by="admin")

    assert result.ok is True
    assert result.chat_title == "ops"
    assert telegram.sent[0]["token"] == BOT_TOKEN
    assert telegram.sent[0]["chat_id"] == CHAT_ID
    assert "test message" in telegram.sent[0]["text"]

    stored = await service.get(result.notification_id or 0)
    assert stored.status == NotificationStatus.SENT.value
    assert stored.event_type == NotificationEvent.TEST.value


async def test_send_test_works_while_notifications_are_switched_off(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    """You configure the token, test it, then switch the feature on — not the other way round."""
    await configure(session, enabled=False)

    result = await build(session, telegram=telegram).send_test()

    assert result.ok is True


async def test_send_test_can_target_a_different_chat(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    await configure(session)

    await build(session, telegram=telegram).send_test(chat_id="-100777")

    assert telegram.sent[0]["chat_id"] == "-100777"


async def test_send_test_returns_telegram_s_refusal_verbatim(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    await configure(session)
    telegram.error = TelegramError(
        "Telegram returned 400 (400): Bad Request: chat not found",
        retryable=False,
        error_code=400,
        description="Bad Request: chat not found",
    )
    service = build(session, telegram=telegram)

    result = await service.send_test()

    assert result.ok is False
    assert result.error_code == 400
    assert result.description == "Bad Request: chat not found"
    stored = await service.get(result.notification_id or 0)
    assert stored.status == NotificationStatus.FAILED.value


async def test_send_test_refuses_without_a_token(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    """A request problem, not a delivery result: there is nothing to attempt."""
    await configure(session, bot_token="")

    with pytest.raises(ValidationFailed, match="bot token"):
        await build(session, telegram=telegram).send_test()


async def test_send_test_refuses_without_a_chat_id(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    await configure(session, chat_id="")

    with pytest.raises(ValidationFailed, match="chat id"):
        await build(session, telegram=telegram).send_test()


async def test_the_bot_token_is_never_stored_in_the_outbox_payload(
    session: AsyncSession, telegram: RecordingTelegram
) -> None:
    await configure(session)
    service = build(session, telegram=telegram)

    result = await service.send_test()
    stored: Notification = await service.get(result.notification_id or 0)

    assert BOT_TOKEN not in str(stored.payload)
