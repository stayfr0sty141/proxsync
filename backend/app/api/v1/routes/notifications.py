"""Outbox inspection, resend, and the Telegram connection test."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    ContainerDep,
    NotificationServiceDep,
    RequireAdmin,
    RequireViewer,
    SessionDep,
)
from app.schemas.enums import NotificationChannel, NotificationEvent, NotificationStatus
from app.schemas.notifications import (
    NotificationListResponse,
    NotificationResendResponse,
    TelegramTestRequest,
    TelegramTestResponse,
)
from app.services.notification_service import to_response

router = APIRouter(prefix="/notifications", tags=["notifications"])

MAX_PAGE_SIZE = 200


@router.get("", summary="Notification outbox")
async def list_notifications(
    service: NotificationServiceDep,
    user: RequireViewer,
    notification_status: Annotated[NotificationStatus | None, Query(alias="status")] = None,
    event_type: NotificationEvent | None = None,
    channel: NotificationChannel | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationListResponse:
    del user
    return await service.list(
        status=notification_status,
        event=event_type,
        channel=channel,
        since=date_from,
        until=date_to,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{notification_id}/resend",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a notification for delivery again",
)
async def resend_notification(
    notification_id: int,
    service: NotificationServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
) -> NotificationResendResponse:
    """202, not 200: the message goes back on the queue and the worker delivers it.

    This is the intended answer to a failed delivery *and* to a suppressed one — the operator
    can see what was withheld and ask for it anyway.
    """
    del user
    record = await service.resend(notification_id)
    container.notification_worker.notify_after_commit(session)
    return NotificationResendResponse(
        notification=to_response(record),
        detail="Queued for delivery.",
    )


@router.post("/telegram/test", summary="Send a test message to Telegram")
async def test_telegram(
    payload: TelegramTestRequest,
    service: NotificationServiceDep,
    user: RequireAdmin,
) -> TelegramTestResponse:
    """Send now and report what Telegram said.

    A rejected message is a 200 with `ok: false`, carrying Telegram's own `error_code` and
    `description`. The HTTP request succeeded; the answer to "is this token right?" is "no,
    and here is exactly why", and flattening that into a problem response would hide the one
    field worth reading.
    """
    return await service.send_test(chat_id=payload.chat_id, requested_by=user.username)
