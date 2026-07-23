"""Notification API shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.enums import NotificationChannel, NotificationEvent, NotificationStatus


class NotificationResponse(BaseModel):
    id: int
    channel: NotificationChannel
    event_type: NotificationEvent
    status: NotificationStatus
    text: str
    """The message as it was rendered when the event happened, not as it would render now."""
    dedupe_key: str | None = None
    suppressed_by: int | None = None
    """The notification this one repeats. Present only on `suppressed` rows."""
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    error_message: str | None = None
    backup_id: int | None = None
    restore_id: int | None = None
    correlation_id: str | None = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    total: int
    pending: int
    """Outstanding deliveries. A number that stays high is an outage, not a backlog."""


class NotificationResendResponse(BaseModel):
    notification: NotificationResponse
    detail: str


class TelegramTestRequest(BaseModel):
    chat_id: str | None = Field(
        default=None,
        description=(
            "Send to this chat instead of the configured one. Lets an admin verify a new chat "
            "id before saving it over a working one."
        ),
    )


class TelegramTestResponse(BaseModel):
    """The result of a delivery attempt, not the outcome of the HTTP request.

    A rejected message is a 200 carrying `ok: false` and Telegram's own words. The request
    itself succeeded — the answer is "Telegram says your token is invalid", and turning that
    into a problem response would hide the one field the operator needs to read.
    """

    ok: bool
    detail: str
    error_code: int | None = None
    description: str | None = None
    """Telegram's `description`, verbatim."""
    chat_title: str | None = None
    message_id: int | None = None
    notification_id: int | None = None
