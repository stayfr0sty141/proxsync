"""`/notifications` — the outbox, resend, and the Telegram connection test."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.notification_repository import SqlAlchemyNotificationRepository
from app.schemas.enums import NotificationChannel, NotificationEvent, NotificationStatus

from .conftest import ApiClient

BOT_TOKEN = "999:test-bot-token"  # noqa: S105 - a fixture, not a credential
CHAT_ID = "-1009999"


class ScriptedTelegramTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response = httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 5, "chat": {"id": -1, "title": "ops"}}},
        )

    def reply(self, status_code: int, payload: Any) -> None:
        self.response = httpx.Response(status_code, json=payload)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(httpx.Response(200, content=request.content).json())
        return self.response


@pytest.fixture
def telegram_transport(client: ApiClient) -> ScriptedTelegramTransport:
    """Point the running app's Telegram client at a stub, leaving its parsing intact."""
    transport = ScriptedTelegramTransport()
    container = client.raw.app.state.container  # type: ignore[attr-defined]
    container.telegram._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        base_url="https://api.telegram.org", transport=transport
    )
    return transport


def configure_telegram(client: ApiClient, **overrides: Any) -> None:
    response = client.put(
        "/api/v1/settings/telegram",
        {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID, **overrides},
    )
    assert response.status_code == 200


async def seed_notification(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event: NotificationEvent = NotificationEvent.BACKUP_FAILED,
    status: NotificationStatus = NotificationStatus.FAILED,
    text: str = "Backup failed",
    dedupe_key: str | None = None,
) -> int:
    async with session_factory() as session:
        record = await SqlAlchemyNotificationRepository(session).create(
            channel=NotificationChannel.TELEGRAM,
            event=event,
            status=status,
            payload={"text": text},
            max_attempts=5,
            dedupe_key=dedupe_key,
        )
        await session.commit()
        return record.id


# ---- listing -----------------------------------------------------------------


async def test_the_outbox_lists_newest_first_with_a_pending_count(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed_notification(session_factory, text="older", status=NotificationStatus.SENT)
    await seed_notification(session_factory, text="newer", status=NotificationStatus.PENDING)

    body = authenticated_client.get("/api/v1/notifications").json()

    assert body["total"] == 2
    assert body["pending"] == 1
    assert body["items"][0]["text"] == "newer"


async def test_the_outbox_filters_by_status_and_event(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await seed_notification(session_factory, status=NotificationStatus.SENT)
    await seed_notification(
        session_factory,
        event=NotificationEvent.STORAGE_THRESHOLD,
        status=NotificationStatus.FAILED,
    )

    failed = authenticated_client.get("/api/v1/notifications?status=failed").json()
    thresholds = authenticated_client.get(
        "/api/v1/notifications?event_type=storage_threshold"
    ).json()

    assert failed["total"] == 1
    assert failed["items"][0]["event_type"] == "storage_threshold"
    assert thresholds["total"] == 1


async def test_a_suppressed_row_names_the_message_it_repeats(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    original = await seed_notification(
        session_factory, status=NotificationStatus.SENT, dedupe_key="storage:warning"
    )
    async with session_factory() as session:
        await SqlAlchemyNotificationRepository(session).create(
            channel=NotificationChannel.TELEGRAM,
            event=NotificationEvent.BACKUP_FAILED,
            status=NotificationStatus.SUPPRESSED,
            payload={"text": "repeat", "suppressed_by": original},
            max_attempts=5,
            dedupe_key="storage:warning",
        )
        await session.commit()

    body = authenticated_client.get("/api/v1/notifications?status=suppressed").json()

    assert body["items"][0]["suppressed_by"] == original


async def test_listing_requires_authentication(client: ApiClient) -> None:
    assert client.get("/api/v1/notifications").status_code == 401


# ---- resend ------------------------------------------------------------------


async def test_resending_a_failed_message_requeues_it(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    notification_id = await seed_notification(session_factory)

    response = authenticated_client.post(f"/api/v1/notifications/{notification_id}/resend")

    assert response.status_code == 202
    assert response.json()["notification"]["status"] == "pending"
    assert response.json()["notification"]["attempts"] == 0


async def test_resending_a_queued_message_is_a_conflict(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    notification_id = await seed_notification(session_factory, status=NotificationStatus.PENDING)

    response = authenticated_client.post(f"/api/v1/notifications/{notification_id}/resend")

    assert response.status_code == 409


async def test_resending_something_that_does_not_exist_is_a_404(
    authenticated_client: ApiClient,
) -> None:
    assert authenticated_client.post("/api/v1/notifications/9999/resend").status_code == 404


# ---- the connection test -----------------------------------------------------


async def test_the_test_message_reports_success(
    authenticated_client: ApiClient, telegram_transport: ScriptedTelegramTransport
) -> None:
    configure_telegram(authenticated_client)

    body = authenticated_client.post("/api/v1/notifications/telegram/test", {}).json()

    assert body["ok"] is True
    assert body["chat_title"] == "ops"
    assert telegram_transport.requests[0]["chat_id"] == CHAT_ID


async def test_a_rejected_test_returns_telegram_s_own_error_with_a_200(
    authenticated_client: ApiClient, telegram_transport: ScriptedTelegramTransport
) -> None:
    """The HTTP request succeeded; the answer is "no, and here is exactly why". Flattening it
    into a problem response would hide the one field worth reading."""
    configure_telegram(authenticated_client)
    telegram_transport.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})

    response = authenticated_client.post("/api/v1/notifications/telegram/test", {})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error_code"] == 401
    assert body["description"] == "Unauthorized"


async def test_the_test_message_can_target_another_chat(
    authenticated_client: ApiClient, telegram_transport: ScriptedTelegramTransport
) -> None:
    """So a new chat id can be verified before it replaces a working one."""
    configure_telegram(authenticated_client)

    authenticated_client.post("/api/v1/notifications/telegram/test", {"chat_id": "-100777"})

    assert telegram_transport.requests[0]["chat_id"] == "-100777"


async def test_testing_without_a_token_is_a_request_error(
    authenticated_client: ApiClient, telegram_transport: ScriptedTelegramTransport
) -> None:
    del telegram_transport
    response = authenticated_client.post("/api/v1/notifications/telegram/test", {})

    assert response.status_code == 400
    assert "bot token" in response.json()["detail"]


async def test_the_test_endpoint_is_admin_only(client: ApiClient) -> None:
    assert client.post("/api/v1/notifications/telegram/test", {}).status_code == 401
