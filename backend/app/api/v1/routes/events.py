"""Server-sent events.

SSE rather than WebSockets: the traffic is one-directional (the server narrates, the browser
listens), it survives an ordinary reverse proxy without an upgrade dance, and `EventSource`
reconnects on its own.

**Authentication is by query token, not by header.** `EventSource` cannot set an
`Authorization` header — that is a browser limitation, not a design choice. The token is a
short-lived access JWT, is never logged (the middleware records paths, not query strings),
and grants exactly what the same token grants anywhere else.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ContainerDep, get_user_repository
from app.core.errors import AuthenticationFailed
from app.core.events import Event, EventBus
from app.core.logging import logger
from app.db.models.user import User
from app.repositories.user_repository import SqlAlchemyUserRepository

router = APIRouter(prefix="/events", tags=["events"])

HEARTBEAT_SECONDS = 20.0
"""A comment line often enough to stop an idle proxy from closing the connection."""


async def _authenticate(request: Request, container: ContainerDep, token: str | None) -> User:
    header = request.headers.get("authorization", "")
    scheme, _, bearer = header.partition(" ")
    candidate = bearer if scheme.lower() == "bearer" and bearer else token
    if not candidate:
        raise AuthenticationFailed(
            "An access token is required. EventSource cannot send an Authorization header, "
            "so pass it as ?token="
        )

    claims = container.tokens.decode_access_token(candidate)
    session = container.database.session_factory()
    try:
        users: SqlAlchemyUserRepository = get_user_repository(session)
        user = await users.get(claims.user_id)
    finally:
        await session.close()

    if user is None or not user.is_active or user.must_change_password:
        raise AuthenticationFailed("This account cannot subscribe to events")
    return user


def _frame(event: Event) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.to_payload(), default=str)}\n\n"


async def event_frames(
    bus: EventBus,
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    label: str = "",
) -> AsyncGenerator[str]:
    """The SSE body.

    Kept out of the route so it can be driven directly by a test: an endpoint that yields
    forever cannot be consumed by a test client without deadlocking it, and "the stream is
    untested" is not an acceptable answer for the only live channel in the application.
    """
    async with bus.subscribe() as queue:
        logger.info("event_stream_opened", subscriber=label)
        # Named so a reconnecting client can tell "I am attached" from "nothing is happening
        # yet" — on a silent stream those look identical.
        yield "event: stream.open\ndata: {}\n\n"
        try:
            while not await is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _frame(event)
        finally:
            logger.info("event_stream_closed", subscriber=label)


@router.get("/stream", summary="Live event stream (SSE)")
async def stream(
    request: Request,
    container: ContainerDep,
    token: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    user = await _authenticate(request, container, token)

    return StreamingResponse(
        event_frames(
            container.events,
            is_disconnected=request.is_disconnected,
            label=user.username,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # nginx buffers proxied responses by default, which would hold progress events
            # until the buffer filled — a progress bar that moves in jumps of nothing.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
