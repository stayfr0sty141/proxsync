"""Notification use cases: what to say, whether to say it again, and what was said.

**The outbox is written in the caller's transaction.** `enqueue` is called from inside the
same session that records the backup, restore or threshold it describes, so the two commit
together. That is the entire reason for the pattern: a notification cannot describe a state
change that was rolled back, and a state change cannot quietly happen unannounced.

**Two kinds of duplicate, two answers.**

* An event that happens once per occurrence — a run finishing, a restore failing — is keyed
  uniquely (`unique=True`). A second enqueue for the same key writes nothing at all, whatever
  the clock says. This is what makes "exactly once" survive a restart: the guarantee lives in
  the database, not in a timer.
* An event that genuinely recurs — a storage threshold, a retention pass — is keyed by what it
  is about and suppressed for a window. The repeat is *recorded* as `suppressed` rather than
  dropped, because "we decided not to tell you at 03:14" is itself something an operator may
  need to see.

Delivery lives in `NotificationWorker`. This service never blocks on the network except in
`send_test`, which is a human pressing a button and waiting for the answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, TelegramError, ValidationFailed
from app.core.logging import get_correlation_id, logger
from app.db.models.system import Notification
from app.repositories.notification_repository import NotificationRepository
from app.schemas.enums import (
    LogCategory,
    NotificationChannel,
    NotificationEvent,
    NotificationStatus,
    SettingsSection,
)
from app.schemas.notifications import (
    NotificationListResponse,
    NotificationResponse,
    TelegramTestResponse,
)
from app.schemas.settings import SectionModel, TelegramSettings
from app.services.notification_templates import render

_TOGGLE_BY_EVENT: dict[NotificationEvent, str | None] = {
    NotificationEvent.BACKUP_STARTED: "notify_backup_started",
    NotificationEvent.BACKUP_SUCCESS: "notify_backup_success",
    NotificationEvent.BACKUP_FAILED: "notify_backup_failed",
    NotificationEvent.RESTORE_STARTED: "notify_restore_started",
    NotificationEvent.RESTORE_FINISHED: "notify_restore_finished",
    NotificationEvent.RESTORE_FAILED: "notify_restore_failed",
    NotificationEvent.UPLOAD_FAILED: "notify_upload_failed",
    NotificationEvent.RETENTION_DELETED: "notify_retention_deleted",
    NotificationEvent.STORAGE_THRESHOLD: "notify_storage_threshold",
    # A test is requested explicitly by an admin; there is nothing to opt out of.
    NotificationEvent.TEST: None,
}


class TelegramSender(Protocol):
    async def send_message(
        self, *, token: str, chat_id: str, text: str, parse_mode: str | None = "HTML"
    ) -> Any: ...


class NotificationSettingsProvider(Protocol):
    async def get_section(self, section: SettingsSection) -> SectionModel: ...


class NotificationSink(Protocol):
    """The narrow view of this service that the workers depend on."""

    async def enqueue(
        self,
        event: NotificationEvent,
        *,
        variables: Mapping[str, Any],
        dedupe_key: str | None = None,
        unique: bool = False,
        backup_id: int | None = None,
        restore_id: int | None = None,
    ) -> Notification | None: ...


class NotificationDoorbell(Protocol):
    def notify_after_commit(self, session: AsyncSession) -> None: ...


@dataclass(frozen=True, slots=True)
class NotificationGateway:
    """What an emitting worker holds instead of a service.

    Two things a worker should not have to know: how to assemble a `NotificationService` from
    a session, and that delivery has to be woken *after* the transaction commits. Both are the
    composition root's business, so they are bound here once and passed in.
    """

    factory: Callable[[AsyncSession], NotificationSink]
    doorbell: NotificationDoorbell | None = None

    async def emit(
        self,
        session: AsyncSession,
        event: NotificationEvent,
        *,
        variables: Mapping[str, Any],
        dedupe_key: str | None = None,
        unique: bool = False,
        backup_id: int | None = None,
        restore_id: int | None = None,
    ) -> Notification | None:
        """Queue one message inside the caller's transaction, then arrange for delivery."""
        record = await self.factory(session).enqueue(
            event,
            variables=variables,
            dedupe_key=dedupe_key,
            unique=unique,
            backup_id=backup_id,
            restore_id=restore_id,
        )
        if (
            record is not None
            and record.status == NotificationStatus.PENDING.value
            and self.doorbell is not None
        ):
            self.doorbell.notify_after_commit(session)
        return record


class NotificationService:
    def __init__(
        self,
        *,
        notifications: NotificationRepository,
        settings_service: NotificationSettingsProvider,
        telegram: TelegramSender | None = None,
        max_attempts: int = 5,
        suppress_window_seconds: int = 300,
    ) -> None:
        self._notifications = notifications
        self._settings = settings_service
        self._telegram = telegram
        self._max_attempts = max_attempts
        self._suppress_window = timedelta(seconds=suppress_window_seconds)

    # ---- enqueue -------------------------------------------------------------

    async def enqueue(
        self,
        event: NotificationEvent,
        *,
        variables: Mapping[str, Any],
        dedupe_key: str | None = None,
        unique: bool = False,
        backup_id: int | None = None,
        restore_id: int | None = None,
    ) -> Notification | None:
        """Write one outbox row, or explain to the log why there is nothing to write."""

        config = await self.telegram_settings()
        if not self._wanted(config, event):
            return None

        channel = NotificationChannel.TELEGRAM
        now = datetime.now(UTC)

        if dedupe_key and unique:
            existing = await self._notifications.latest_with_key(
                channel=channel, event=event, dedupe_key=dedupe_key
            )
            if existing is not None:
                # Not even a suppressed row: this occurrence is already in the outbox, and a
                # second row per restart would turn the record of it into a counter of crashes.
                logger.debug(
                    "notification_already_recorded",
                    category=LogCategory.NOTIFY.value,
                    event_type=event.value,
                    dedupe_key=dedupe_key,
                    notification_id=existing.id,
                )
                return None

        suppressed_by: int | None = None
        if dedupe_key and not unique:
            recent = await self._notifications.latest_with_key(
                channel=channel,
                event=event,
                dedupe_key=dedupe_key,
                since=now - self._suppress_window,
                statuses=(NotificationStatus.PENDING, NotificationStatus.SENT),
            )
            suppressed_by = recent.id if recent is not None else None

        text = render(event, variables)
        payload: dict[str, Any] = {"text": text, "variables": dict(variables)}
        if suppressed_by is not None:
            payload["suppressed_by"] = suppressed_by
            payload["suppressed_reason"] = (
                f"An identical {event.value} notification was queued or sent within the last "
                f"{int(self._suppress_window.total_seconds())}s"
            )

        record = await self._notifications.create(
            channel=channel,
            event=event,
            status=(
                NotificationStatus.SUPPRESSED
                if suppressed_by is not None
                else NotificationStatus.PENDING
            ),
            payload=payload,
            max_attempts=self._max_attempts,
            dedupe_key=dedupe_key,
            backup_id=backup_id,
            restore_id=restore_id,
            correlation_id=get_correlation_id(),
        )
        logger.info(
            "notification_queued" if suppressed_by is None else "notification_suppressed",
            category=LogCategory.NOTIFY.value,
            notification_id=record.id,
            event_type=event.value,
            dedupe_key=dedupe_key,
            suppressed_by=suppressed_by,
            backup_id=backup_id,
            restore_id=restore_id,
        )
        return record

    @staticmethod
    def _wanted(config: TelegramSettings, event: NotificationEvent) -> bool:
        if not config.enabled:
            logger.debug(
                "notification_skipped_channel_disabled",
                category=LogCategory.NOTIFY.value,
                event_type=event.value,
            )
            return False
        toggle = _TOGGLE_BY_EVENT[event]
        if toggle is not None and not getattr(config, toggle):
            logger.debug(
                "notification_skipped_event_disabled",
                category=LogCategory.NOTIFY.value,
                event_type=event.value,
                setting=toggle,
            )
            return False
        return True

    # ---- reads ---------------------------------------------------------------

    async def list(
        self,
        *,
        status: NotificationStatus | None = None,
        event: NotificationEvent | None = None,
        channel: NotificationChannel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> NotificationListResponse:
        items = await self._notifications.search(
            status=status,
            event=event,
            channel=channel,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        total = await self._notifications.count(
            status=status, event=event, channel=channel, since=since, until=until
        )
        return NotificationListResponse(
            items=[to_response(item) for item in items],
            total=total,
            pending=await self._notifications.pending_count(),
        )

    async def get(self, notification_id: int) -> Notification:
        record = await self._notifications.get(notification_id)
        if record is None:
            raise NotFound(f"Notification #{notification_id} does not exist")
        return record

    # ---- operator actions ----------------------------------------------------

    async def resend(self, notification_id: int) -> Notification:
        """Put a message back on the queue.

        A `pending` row is refused rather than reset: it is already queued, and clearing its
        attempt count would restart a backoff that exists precisely because Telegram is
        currently refusing it.
        """
        record = await self.get(notification_id)
        if record.status == NotificationStatus.PENDING.value:
            raise Conflict(
                f"Notification #{notification_id} is still queued for delivery "
                f"(attempt {record.attempts} of {record.max_attempts})."
            )

        record.status = NotificationStatus.PENDING.value
        record.attempts = 0
        record.next_attempt_at = None
        record.error_message = None
        record.sent_at = None
        logger.info(
            "notification_requeued",
            category=LogCategory.NOTIFY.value,
            notification_id=notification_id,
            event_type=record.event_type,
        )
        return record

    async def send_test(
        self, *, chat_id: str | None = None, requested_by: str | None = None
    ) -> TelegramTestResponse:
        """Send a message now and report exactly what Telegram said.

        Deliberately not queued: the operator is standing in front of the settings page, and
        "it will be attempted shortly" is not an answer to "is this token right?".
        """
        if self._telegram is None:  # pragma: no cover - wired in every real composition
            raise ValidationFailed("No Telegram client is configured in this process")

        config = await self.telegram_settings()
        token = (config.bot_token or "").strip()
        target = (chat_id or config.chat_id or "").strip()
        if not token:
            raise ValidationFailed(
                "No Telegram bot token is configured. Save one in Settings → Telegram first."
            )
        if not target:
            raise ValidationFailed(
                "No Telegram chat id is configured. Save one, or pass `chat_id` to test "
                "against a different chat."
            )

        text = render(NotificationEvent.TEST, {"requested_by": requested_by})
        # The send happens before any write, so this request never holds a database writer open
        # across a network call — the same rule the storage sampler follows.
        try:
            message = await self._telegram.send_message(token=token, chat_id=target, text=text)
        except TelegramError as exc:
            record = await self._record_test(text, chat_id=target, error=str(exc.detail))
            logger.warning(
                "notification_test_failed",
                category=LogCategory.NOTIFY.value,
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
            return TelegramTestResponse(
                ok=False,
                detail=str(exc.detail),
                error_code=exc.error_code,
                description=exc.description,
                notification_id=record.id,
            )

        record = await self._record_test(text, chat_id=target, error=None)
        logger.info(
            "notification_test_sent",
            category=LogCategory.NOTIFY.value,
            notification_id=record.id,
        )
        return TelegramTestResponse(
            ok=True,
            detail=f"Test message delivered to {message.chat_title or target}.",
            chat_title=message.chat_title,
            message_id=message.message_id,
            notification_id=record.id,
        )

    async def _record_test(self, text: str, *, chat_id: str, error: str | None) -> Notification:
        """A test is a real notification with a known outcome, so it belongs in the outbox."""
        return await self._notifications.create(
            channel=NotificationChannel.TELEGRAM,
            event=NotificationEvent.TEST,
            status=NotificationStatus.FAILED if error else NotificationStatus.SENT,
            payload={"text": text, "variables": {"chat_id": chat_id}},
            max_attempts=self._max_attempts,
            correlation_id=get_correlation_id(),
        )

    # ---- settings ------------------------------------------------------------

    async def telegram_settings(self) -> TelegramSettings:
        section = await self._settings.get_section(SettingsSection.TELEGRAM)
        assert isinstance(section, TelegramSettings)
        return section


def to_response(record: Notification) -> NotificationResponse:
    payload = record.payload or {}
    suppressed_by = payload.get("suppressed_by")
    return NotificationResponse(
        id=record.id,
        channel=NotificationChannel(record.channel),
        event_type=NotificationEvent(record.event_type),
        status=NotificationStatus(record.status),
        text=str(payload.get("text", "")),
        dedupe_key=record.dedupe_key,
        suppressed_by=suppressed_by if isinstance(suppressed_by, int) else None,
        attempts=record.attempts,
        max_attempts=record.max_attempts,
        next_attempt_at=record.next_attempt_at,
        sent_at=record.sent_at,
        error_message=record.error_message,
        backup_id=record.backup_id,
        restore_id=record.restore_id,
        correlation_id=record.correlation_id,
        created_at=record.created_at,
    )
