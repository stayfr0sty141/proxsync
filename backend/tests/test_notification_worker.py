"""Outbox delivery: ordering, retries, and what is never retried.

The interesting behaviour is failure. A message that cannot be delivered must not be lost, a
message that can never be delivered must not consume the queue forever, and a process that
dies mid-send must not leave a row claiming an attempt that never happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.clients.telegram_client import TelegramClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EVENT_NOTIFICATION_STATE, EventBus
from app.db.models.system import Notification
from app.db.session import Database
from app.repositories.notification_repository import SqlAlchemyNotificationRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import (
    NotificationChannel,
    NotificationEvent,
    NotificationStatus,
    SettingsSection,
)
from app.services.settings_service import SettingsService
from app.workers.notification_worker import NotificationWorker

from .conftest import SECRET_KEY

BOT_TOKEN = "999:test-bot-token"  # noqa: S105 - a fixture, not a credential
CHAT_ID = "-1009999"


class ScriptedTelegramTransport(httpx.AsyncBaseTransport):
    """Answers `sendMessage` from a scripted queue of responses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[httpx.Response] = []
        self.raise_on_request: Exception | None = None

    def queue(self, status_code: int, payload: Any) -> None:
        self.responses.append(httpx.Response(status_code, json=payload))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(httpx.Response(200, content=request.content).json())
        if self.raise_on_request is not None:
            raise self.raise_on_request
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": -1, "title": "ops"}}}
        )


@pytest.fixture
def transport() -> ScriptedTelegramTransport:
    return ScriptedTelegramTransport()


@pytest.fixture
def telegram(settings: Settings, transport: ScriptedTelegramTransport) -> TelegramClient:
    return TelegramClient(
        settings,
        client=httpx.AsyncClient(base_url="https://api.telegram.org", transport=transport),
    )


@pytest.fixture
async def database(settings: Settings, prepared_database: None) -> AsyncIterator[Database]:
    del prepared_database
    instance = Database(settings)
    yield instance
    await instance.dispose()


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def worker(
    settings: Settings, database: Database, telegram: TelegramClient, events: EventBus
) -> NotificationWorker:
    fast = settings.model_copy(
        update={
            "notification_idle_poll_seconds": 0.01,
            "notification_retry_base_seconds": 30,
            "notification_retry_max_seconds": 3600,
        }
    )
    return NotificationWorker(
        database=database,
        telegram=telegram,
        events=events,
        secret_box=SecretBox(SECRET_KEY),
        settings=fast,
    )


async def configure(database: Database, **overrides: Any) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        await service.update_section(
            SettingsSection.TELEGRAM,
            {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID, **overrides},
        )


async def queue_message(
    database: Database,
    *,
    text: str = "hello",
    status: NotificationStatus = NotificationStatus.PENDING,
    attempts: int = 0,
    max_attempts: int = 3,
    created_at: datetime | None = None,
) -> int:
    async with database.session() as session:
        record = await SqlAlchemyNotificationRepository(session).create(
            channel=NotificationChannel.TELEGRAM,
            event=NotificationEvent.BACKUP_FAILED,
            status=status,
            payload={"text": text},
            max_attempts=max_attempts,
        )
        record.attempts = attempts
        if created_at is not None:
            record.created_at = created_at
        return record.id


async def load(database: Database, notification_id: int) -> Notification:
    async with database.session() as session:
        record = await SqlAlchemyNotificationRepository(session).get(notification_id)
        assert record is not None
        session.expunge(record)
        return record


async def run_one(worker: NotificationWorker) -> None:
    """One claim-and-deliver cycle, without the loop's timing."""
    claim = await worker._claim_next()  # noqa: SLF001 - the worker's own unit of work
    assert claim is not None
    await worker._deliver(claim)  # noqa: SLF001


# ---- the happy path ----------------------------------------------------------


async def test_a_pending_message_is_delivered_and_marked_sent(
    worker: NotificationWorker,
    database: Database,
    transport: ScriptedTelegramTransport,
    events: EventBus,
) -> None:
    await configure(database)
    notification_id = await queue_message(database, text="<b>Backup failed</b>")

    async with events.subscribe() as queue:
        await run_one(worker)
        published = queue.get_nowait()

    assert transport.requests[0]["chat_id"] == CHAT_ID
    assert transport.requests[0]["text"] == "<b>Backup failed</b>"
    record = await load(database, notification_id)
    assert record.status == NotificationStatus.SENT.value
    assert record.sent_at is not None
    assert record.next_attempt_at is None
    assert record.error_message is None
    assert published.type == EVENT_NOTIFICATION_STATE
    assert published.data["status"] == NotificationStatus.SENT.value


async def test_the_queue_is_oldest_first(worker: NotificationWorker, database: Database) -> None:
    """A message queued during an outage must not be starved by ones queued after it clears."""
    await configure(database)
    now = datetime.now(UTC)
    older = await queue_message(database, text="first", created_at=now - timedelta(minutes=10))
    await queue_message(database, text="second", created_at=now)

    claim = await worker._claim_next()  # noqa: SLF001

    assert claim is not None
    assert claim.notification_id == older


async def test_a_suppressed_row_is_never_claimed(
    worker: NotificationWorker, database: Database
) -> None:
    await configure(database)
    await queue_message(database, status=NotificationStatus.SUPPRESSED)

    assert await worker._claim_next() is None  # noqa: SLF001


async def test_a_message_waiting_on_backoff_is_not_ready_yet(
    worker: NotificationWorker, database: Database
) -> None:
    await configure(database)
    notification_id = await queue_message(database)
    async with database.session() as session:
        record = await SqlAlchemyNotificationRepository(session).get(notification_id)
        assert record is not None
        record.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)

    assert await worker._claim_next() is None  # noqa: SLF001


# ---- at-least-once -----------------------------------------------------------


async def test_the_attempt_is_charged_before_the_send(
    worker: NotificationWorker, database: Database
) -> None:
    """A process that dies mid-request leaves a row with a scheduled retry, so the message is
    redelivered rather than stuck claiming an attempt that never happened."""
    await configure(database)
    notification_id = await queue_message(database)

    claim = await worker._claim_next()  # noqa: SLF001
    assert claim is not None

    record = await load(database, notification_id)
    assert record.attempts == 1
    assert record.status == NotificationStatus.PENDING.value
    assert record.next_attempt_at is not None


# ---- failure handling --------------------------------------------------------


async def test_a_transient_failure_is_requeued_with_backoff(
    worker: NotificationWorker, database: Database, transport: ScriptedTelegramTransport
) -> None:
    await configure(database)
    transport.queue(503, {"ok": False, "error_code": 503, "description": "try later"})
    notification_id = await queue_message(database)

    await run_one(worker)

    record = await load(database, notification_id)
    assert record.status == NotificationStatus.PENDING.value
    assert record.attempts == 1
    assert record.next_attempt_at is not None
    assert "503" in (record.error_message or "")


async def test_a_refusal_is_terminal_on_the_first_attempt(
    worker: NotificationWorker, database: Database, transport: ScriptedTelegramTransport
) -> None:
    """`chat not found` will fail identically forever; spending five attempts on it would only
    delay the failure an operator needs to see."""
    await configure(database)
    transport.queue(
        400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    )
    notification_id = await queue_message(database, max_attempts=5)

    await run_one(worker)

    record = await load(database, notification_id)
    assert record.status == NotificationStatus.FAILED.value
    assert record.attempts == 1
    assert record.next_attempt_at is None
    assert "chat not found" in (record.error_message or "")


async def test_retries_stop_at_max_attempts(
    worker: NotificationWorker, database: Database, transport: ScriptedTelegramTransport
) -> None:
    await configure(database)
    notification_id = await queue_message(database, attempts=2, max_attempts=3)
    transport.queue(503, {"ok": False, "error_code": 503, "description": "try later"})

    await run_one(worker)

    record = await load(database, notification_id)
    assert record.attempts == 3
    assert record.status == NotificationStatus.FAILED.value
    assert record.next_attempt_at is None


async def test_telegram_s_own_backoff_wins_over_the_computed_one(
    worker: NotificationWorker, database: Database, transport: ScriptedTelegramTransport
) -> None:
    """Guessing shorter earns another 429; guessing longer delays everything behind it."""
    await configure(database)
    transport.queue(
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 5},
        },
    )
    notification_id = await queue_message(database)
    before = datetime.now(UTC)

    await run_one(worker)

    record = await load(database, notification_id)
    assert record.next_attempt_at is not None
    waited = (record.next_attempt_at - before).total_seconds()
    # 5s from Telegram, not the 30s+ the exponential backoff would have chosen.
    assert 4 <= waited <= 8


async def test_an_unreachable_api_is_retried(
    worker: NotificationWorker, database: Database, transport: ScriptedTelegramTransport
) -> None:
    await configure(database)
    transport.raise_on_request = httpx.ConnectError("no route to host")
    notification_id = await queue_message(database)

    await run_one(worker)

    record = await load(database, notification_id)
    assert record.status == NotificationStatus.PENDING.value
    assert record.next_attempt_at is not None


# ---- configuration -----------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"bot_token": ""}, "no bot token"),
        ({"chat_id": ""}, "no chat id"),
        ({"enabled": False}, "notifications are disabled"),
    ],
)
async def test_missing_configuration_fails_without_burning_retries(
    worker: NotificationWorker,
    database: Database,
    transport: ScriptedTelegramTransport,
    overrides: dict[str, Any],
    expected: str,
) -> None:
    """Retrying cannot fix a missing token, and the attempt budget is not the place to store
    "this was never set up". Resend is the way back once it is."""
    await configure(database, **overrides)
    notification_id = await queue_message(database, max_attempts=5)

    await run_one(worker)

    record = await load(database, notification_id)
    assert record.status == NotificationStatus.FAILED.value
    assert expected in (record.error_message or "")
    assert transport.requests == []


# ---- the loop ----------------------------------------------------------------


async def test_the_running_worker_drains_the_queue(
    worker: NotificationWorker, database: Database, transport: ScriptedTelegramTransport
) -> None:
    await configure(database)
    first = await queue_message(database, text="one")
    second = await queue_message(database, text="two")

    await worker.start()
    try:
        for _ in range(200):
            if len(transport.requests) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()

    assert (await load(database, first)).status == NotificationStatus.SENT.value
    assert (await load(database, second)).status == NotificationStatus.SENT.value


async def test_a_message_left_pending_by_a_restart_is_picked_back_up(
    worker: NotificationWorker, database: Database
) -> None:
    """The outbox needs no recovery step: a pending row *is* the recovery state."""
    await configure(database)
    notification_id = await queue_message(database, attempts=1)

    await worker.start()
    try:
        for _ in range(200):
            if (await load(database, notification_id)).status == NotificationStatus.SENT.value:
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()

    record = await load(database, notification_id)
    assert record.status == NotificationStatus.SENT.value
    # The attempt count carries over from the previous process rather than restarting.
    assert record.attempts == 2
