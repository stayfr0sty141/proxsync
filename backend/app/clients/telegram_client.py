"""Telegram Bot API client.

The only egress path in the dashboard that is not the agent or Proxmox. It sends messages and
nothing else: there is no `getUpdates`, no webhook, no inbound command surface. A bot that can
only speak cannot be told to do anything by whoever finds the chat.

**Errors are passed through, not summarised.** "Telegram rejected the message" is useless to
an operator; `400 Bad Request: chat not found` tells them exactly which field is wrong. The
API's own `error_code` and `description` reach the settings page verbatim, and the retryable /
terminal split is derived here so the outbox worker does not have to interpret them again.

**The bot token is a URL path segment**, which makes it the one credential that can leak
through ordinary request logging. Every log line here goes through `_redact`.
"""

from __future__ import annotations

import re
from typing import Any, Self

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import TelegramError
from app.core.logging import logger

MAX_MESSAGE_LENGTH = 4096
"""Telegram's hard limit. A longer message is rejected outright, so it is truncated here
instead: a backup report that arrives with its tail cut is better than one that never arrives.
"""

_TOKEN_IN_PATH = re.compile(r"/bot[^/]+/")

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class TelegramMessage(BaseModel):
    """The parts of `sendMessage`'s result the dashboard has any use for."""

    message_id: int | None = None
    chat_id: str | None = None
    chat_title: str | None = None


def _redact(url: str) -> str:
    return _TOKEN_IN_PATH.sub("/bot<redacted>/", url)


def truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[: MAX_MESSAGE_LENGTH - 1] + "…"


class TelegramClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=settings.telegram_api_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.telegram_timeout_seconds, connect=5.0),
            headers={"User-Agent": "proxsync-dashboard"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(
        self, *, token: str, chat_id: str, text: str, parse_mode: str | None = "HTML"
    ) -> TelegramMessage:
        """Deliver one message. Raises `TelegramError` for every unhappy path."""

        path = f"/bot{token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": truncate(text),
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            # A transport failure says nothing about the message, so it is always retryable.
            logger.warning("telegram_request_failed", url=_redact(path), error=str(exc))
            raise TelegramError(
                f"Could not reach the Telegram API at {self._settings.telegram_api_base_url}: "
                f"{exc}",
                retryable=True,
            ) from exc

        return self._parse(response)

    def _parse(self, response: httpx.Response) -> TelegramMessage:
        body = self._body(response)
        if response.is_success and body.get("ok") is True:
            return _message_from(body.get("result"))

        error_code = body.get("error_code")
        description = body.get("description")
        retry_after = _retry_after(body)

        # Telegram answers 4xx with a body that names the problem; 5xx and 429 mean "not now".
        retryable = response.status_code in _RETRYABLE_STATUS or response.status_code >= 500
        detail = (
            f"Telegram returned {response.status_code}"
            f"{f' ({error_code})' if isinstance(error_code, int) else ''}"
            f": {description or response.text[:300] or 'no description'}"
        )
        logger.warning(
            "telegram_send_rejected",
            status=response.status_code,
            error_code=error_code,
            retryable=retryable,
        )
        raise TelegramError(
            detail,
            retryable=retryable,
            error_code=error_code if isinstance(error_code, int) else None,
            description=description if isinstance(description, str) else None,
            retry_after=retry_after,
        )

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            # A proxy or captive portal answering instead of Telegram. Not a message problem.
            return {}
        return body if isinstance(body, dict) else {}


def _retry_after(body: dict[str, Any]) -> int | None:
    """Telegram's own backoff instruction, in `parameters.retry_after` seconds.

    Honoured in preference to the computed backoff: guessing shorter earns another 429, and
    guessing longer delays every other message queued behind it.
    """
    parameters = body.get("parameters")
    if isinstance(parameters, dict):
        value = parameters.get("retry_after")
        if isinstance(value, int) and value > 0:
            return value
    return None


def _message_from(result: Any) -> TelegramMessage:
    if not isinstance(result, dict):
        return TelegramMessage()
    chat = result.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    title = chat.get("title") or chat.get("username") if isinstance(chat, dict) else None
    message_id = result.get("message_id")
    return TelegramMessage(
        message_id=message_id if isinstance(message_id, int) else None,
        chat_id=str(chat_id) if chat_id is not None else None,
        chat_title=str(title) if title is not None else None,
    )
