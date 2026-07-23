"""The Telegram client's request shape, error passthrough and token handling."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.clients.telegram_client import (
    MAX_MESSAGE_LENGTH,
    TelegramClient,
    _redact,
    truncate,
)
from app.core.config import Settings
from app.core.errors import TelegramError

TOKEN = "123456:AAH-super-secret-bot-token"  # noqa: S105 - a fixture, not a credential
CHAT = "-1001234567890"


class ScriptedTelegram(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 42, "chat": {"id": -1001234567890, "title": "ops"}},
            },
        )
        self.raise_on_request: Exception | None = None

    def reply(self, status_code: int, payload: Any) -> None:
        self.response = httpx.Response(status_code, json=payload)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on_request is not None:
            raise self.raise_on_request
        return self.response


@pytest.fixture
def transport() -> ScriptedTelegram:
    return ScriptedTelegram()


@pytest.fixture
def client(settings: Settings, transport: ScriptedTelegram) -> TelegramClient:
    return TelegramClient(
        settings,
        client=httpx.AsyncClient(base_url="https://api.telegram.org", transport=transport),
    )


async def test_send_message_posts_the_expected_payload(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    message = await client.send_message(token=TOKEN, chat_id=CHAT, text="<b>hello</b>")

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/bot{TOKEN}/sendMessage"

    body = httpx.Response(200, content=request.content).json()
    assert body == {
        "chat_id": CHAT,
        "text": "<b>hello</b>",
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    assert message.message_id == 42
    assert message.chat_title == "ops"


async def test_a_refusal_carries_telegram_s_own_words(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    transport.reply(
        400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    )

    with pytest.raises(TelegramError) as excinfo:
        await client.send_message(token=TOKEN, chat_id=CHAT, text="hi")

    error = excinfo.value
    assert error.error_code == 400
    assert error.description == "Bad Request: chat not found"
    assert "chat not found" in error.detail
    # A wrong chat id fails identically forever; retrying would only burn the attempt budget.
    assert error.retryable is False


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
async def test_transient_upstream_failures_are_retryable(
    client: TelegramClient, transport: ScriptedTelegram, status_code: int
) -> None:
    transport.reply(status_code, {"ok": False, "error_code": status_code, "description": "later"})

    with pytest.raises(TelegramError) as excinfo:
        await client.send_message(token=TOKEN, chat_id=CHAT, text="hi")

    assert excinfo.value.retryable is True


async def test_rate_limit_surfaces_telegram_s_own_backoff(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    transport.reply(
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 37",
            "parameters": {"retry_after": 37},
        },
    )

    with pytest.raises(TelegramError) as excinfo:
        await client.send_message(token=TOKEN, chat_id=CHAT, text="hi")

    assert excinfo.value.retry_after == 37


async def test_a_transport_failure_is_always_retryable(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    transport.raise_on_request = httpx.ConnectError("no route to host")

    with pytest.raises(TelegramError) as excinfo:
        await client.send_message(token=TOKEN, chat_id=CHAT, text="hi")

    # The message may or may not have arrived, so the only safe reading is "try again".
    assert excinfo.value.retryable is True
    assert excinfo.value.error_code is None


async def test_a_non_json_body_does_not_crash_the_parser(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    """A captive portal or proxy answering instead of Telegram."""
    transport.response = httpx.Response(502, text="<html>Bad Gateway</html>")

    with pytest.raises(TelegramError) as excinfo:
        await client.send_message(token=TOKEN, chat_id=CHAT, text="hi")

    assert excinfo.value.retryable is True
    assert "502" in excinfo.value.detail


async def test_ok_false_with_a_200_is_still_a_failure(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    transport.reply(200, {"ok": False, "error_code": 403, "description": "bot was blocked"})

    with pytest.raises(TelegramError) as excinfo:
        await client.send_message(token=TOKEN, chat_id=CHAT, text="hi")

    assert excinfo.value.description == "bot was blocked"


async def test_an_over_long_message_is_truncated_rather_than_rejected(
    client: TelegramClient, transport: ScriptedTelegram
) -> None:
    await client.send_message(token=TOKEN, chat_id=CHAT, text="x" * (MAX_MESSAGE_LENGTH + 500))

    body = httpx.Response(200, content=transport.requests[0].content).json()
    assert len(body["text"]) == MAX_MESSAGE_LENGTH
    assert body["text"].endswith("…")


def test_truncate_leaves_a_short_message_alone() -> None:
    assert truncate("short") == "short"


def test_the_token_never_appears_in_a_logged_url() -> None:
    """The token is a path segment, which makes it the one credential ordinary request
    logging would leak."""
    assert _redact(f"/bot{TOKEN}/sendMessage") == "/bot<redacted>/sendMessage"
    assert TOKEN not in _redact(f"/bot{TOKEN}/sendMessage")
