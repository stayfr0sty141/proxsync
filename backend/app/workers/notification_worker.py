"""The outbox worker: delivers what the rest of the application decided to say.

**The database is the queue**, as everywhere else. A message is durable before anything is
sent, so a Telegram outage delays notifications and a crash loses none.

**Delivery is at-least-once, and that is the right choice here.** The attempt counter is
incremented *before* the send, so a process that dies mid-request leaves a row whose backoff
has already been scheduled — it is retried rather than stuck. A message the operator receives
twice is a minor annoyance; a failure they are never told about is the thing this feature
exists to prevent. That trade is the opposite of the one backups and restores make, because
sending a message twice does not destroy anything.

**Not every failure deserves a retry.** `400 chat not found` will fail identically forever and
would burn the attempt budget, so the client's `retryable` flag decides: a transport error, a
429 or a 5xx goes back on the queue, and a refusal is terminal on the first attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.clients.telegram_client import TelegramClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import TelegramError
from app.core.events import EVENT_NOTIFICATION_STATE, EventBus
from app.core.logging import logger, set_correlation_id
from app.db.session import Database
from app.repositories.notification_repository import SqlAlchemyNotificationRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import LogCategory, NotificationStatus, SettingsSection
from app.schemas.settings import TelegramSettings
from app.services.settings_service import SettingsService


@dataclass(frozen=True, slots=True)
class _Claim:
    """A message taken off the queue, with everything needed to send it."""

    notification_id: int
    event_type: str
    text: str
    attempt: int
    max_attempts: int
    correlation_id: str | None


class NotificationWorker:
    def __init__(
        self,
        *,
        database: Database,
        telegram: TelegramClient,
        events: EventBus,
        secret_box: SecretBox,
        settings: Settings,
    ) -> None:
        self._database = database
        self._telegram = telegram
        self._events = events
        self._secret_box = secret_box
        self._settings = settings

        self._task: asyncio.Task[None] | None = None
        self._doorbell = asyncio.Event()
        self._stopping = asyncio.Event()
        self._last_error: str | None = None

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="proxsync-notification-worker")
        # Anything left pending by the previous process is picked up by the first pass; the
        # outbox needs no recovery step, because a pending row *is* the recovery state.
        self.notify()
        logger.info("notification_worker_started")

    async def stop(self) -> None:
        self._stopping.set()
        self._doorbell.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._task, timeout=10)
            self._task = None
        logger.info("notification_worker_stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def notify(self) -> None:
        self._doorbell.set()

    def notify_after_commit(self, session: AsyncSession) -> None:
        """Ring the doorbell once the transaction holding the new row commits.

        Calling `notify()` directly would race the caller's own commit: the worker could query
        before the outbox row was visible and go back to sleep with a message queued.
        """

        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _ring(_session: Session) -> None:
            self._doorbell.set()

    # ---- queue ---------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            # Cleared before claiming, so a notify arriving during a send is not lost.
            self._doorbell.clear()
            try:
                claim = await self._claim_next()
            except Exception:  # noqa: BLE001 - the worker must outlive a bad iteration
                logger.error("notification_claim_failed", exc_info=True)
                await self._sleep(self._settings.notification_idle_poll_seconds)
                continue

            if claim is None:
                await self._wait_for_work()
                continue

            try:
                await self._deliver(claim)
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the queue
                logger.error(
                    "notification_delivery_crashed",
                    category=LogCategory.NOTIFY.value,
                    notification_id=claim.notification_id,
                    exc_info=True,
                )
                await self._record_failure(claim, f"The outbox worker failed: {exc}", retry=True)

    async def _wait_for_work(self) -> None:
        """Sleep until the doorbell rings or the idle poll comes round.

        The poll also drives the retry schedule: a message waiting on backoff becomes ready
        without anything ringing a bell for it.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._doorbell.wait(), timeout=self._settings.notification_idle_poll_seconds
            )

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _claim_next(self) -> _Claim | None:
        """Take the oldest ready message and charge it an attempt before it is sent.

        Charging first is what makes a lost process safe: the row already carries a scheduled
        retry, so it is redelivered rather than left claiming an attempt that never happened.
        """
        async with self._database.session() as session:
            notifications = SqlAlchemyNotificationRepository(session)
            record = await notifications.next_ready(now=datetime.now(UTC))
            if record is None:
                return None

            record.attempts += 1
            record.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=self._retry_delay(record.attempts)
            )
            await notifications.flush()
            payload = record.payload or {}
            return _Claim(
                notification_id=record.id,
                event_type=record.event_type,
                text=str(payload.get("text", "")),
                attempt=record.attempts,
                max_attempts=record.max_attempts,
                correlation_id=record.correlation_id,
            )

    # ---- delivery ------------------------------------------------------------

    async def _deliver(self, claim: _Claim) -> None:
        set_correlation_id(claim.correlation_id or None)
        config = await self._telegram_settings()

        token = (config.bot_token or "").strip()
        target = (config.chat_id or "").strip()
        if not config.enabled or not token or not target:
            # Retrying cannot fix a missing token, and the attempt budget is not the right
            # place to store "this was never configured". Resend is available once it is.
            await self._record_failure(
                claim,
                "Telegram is not fully configured: "
                + ", ".join(
                    reason
                    for reason in (
                        None if config.enabled else "notifications are disabled",
                        None if token else "no bot token is saved",
                        None if target else "no chat id is saved",
                    )
                    if reason
                )
                + ". Fix it in Settings → Telegram, then resend.",
                retry=False,
            )
            return

        if not claim.text.strip():  # pragma: no cover - templates never render empty
            await self._record_failure(claim, "The message had no text to send", retry=False)
            return

        # No database session is open across the send: a Telegram timeout must not hold
        # SQLite's single writer for fifteen seconds.
        try:
            message = await self._telegram.send_message(
                token=token, chat_id=target, text=claim.text
            )
        except TelegramError as exc:
            await self._record_failure(
                claim,
                str(exc.detail),
                retry=exc.retryable,
                retry_after=exc.retry_after,
            )
            return

        await self._record_sent(claim, chat_title=message.chat_title)

    async def _record_sent(self, claim: _Claim, *, chat_title: str | None) -> None:
        async with self._database.session() as session:
            record = await SqlAlchemyNotificationRepository(session).get(claim.notification_id)
            if record is None:  # pragma: no cover - claimed by this worker
                return
            record.status = NotificationStatus.SENT.value
            record.sent_at = datetime.now(UTC)
            record.next_attempt_at = None
            record.error_message = None

        self._last_error = None
        logger.info(
            "notification_sent",
            category=LogCategory.NOTIFY.value,
            notification_id=claim.notification_id,
            event_type=claim.event_type,
            attempt=claim.attempt,
            chat=chat_title,
        )
        self._events.publish(
            EVENT_NOTIFICATION_STATE,
            notification_id=claim.notification_id,
            event_type=claim.event_type,
            status=NotificationStatus.SENT.value,
        )

    async def _record_failure(
        self,
        claim: _Claim,
        message: str,
        *,
        retry: bool,
        retry_after: int | None = None,
    ) -> None:
        """Schedule another attempt, or give up and say why."""
        exhausted = not retry or claim.attempt >= claim.max_attempts

        async with self._database.session() as session:
            record = await SqlAlchemyNotificationRepository(session).get(claim.notification_id)
            if record is None:  # pragma: no cover
                return
            record.error_message = message[:2000]
            if exhausted:
                record.status = NotificationStatus.FAILED.value
                record.next_attempt_at = None
            else:
                record.status = NotificationStatus.PENDING.value
                # Telegram's own `retry_after` beats the computed backoff: guessing shorter
                # earns another 429, and guessing longer delays everything queued behind it.
                delay = (
                    float(retry_after)
                    if retry_after is not None
                    else self._retry_delay(claim.attempt)
                )
                record.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            next_attempt_at = record.next_attempt_at

        self._last_error = message
        log = logger.error if exhausted else logger.warning
        log(
            "notification_delivery_failed",
            category=LogCategory.NOTIFY.value,
            notification_id=claim.notification_id,
            event_type=claim.event_type,
            attempt=claim.attempt,
            max_attempts=claim.max_attempts,
            retry_at=next_attempt_at.isoformat() if next_attempt_at else None,
            error=message[:200],
        )
        self._events.publish(
            EVENT_NOTIFICATION_STATE,
            notification_id=claim.notification_id,
            event_type=claim.event_type,
            status=(
                NotificationStatus.FAILED.value if exhausted else NotificationStatus.PENDING.value
            ),
            attempt=claim.attempt,
            max_attempts=claim.max_attempts,
            error=message[:200],
        )

    def _retry_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, as for uploads.

        Jittered because a Telegram outage makes every queued message ready at the same
        instant, and retrying them in lockstep reproduces the burst.
        """
        base = self._settings.notification_retry_base_seconds
        ceiling = min(
            self._settings.notification_retry_max_seconds, base * (2 ** max(attempt - 1, 0))
        )
        return random.uniform(base, ceiling) if ceiling > base else float(base)  # noqa: S311

    async def _telegram_settings(self) -> TelegramSettings:
        async with self._database.session() as session:
            section = await SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
            ).get_section(SettingsSection.TELEGRAM)
        assert isinstance(section, TelegramSettings)
        return section
