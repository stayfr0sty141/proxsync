"""The SSE endpoint.

The stream itself is driven through `event_frames` rather than through the test client: an
endpoint that yields until the client disconnects cannot be consumed by `TestClient` without
deadlocking it, and leaving the only live channel in the application untested is not an
acceptable trade.
"""

from __future__ import annotations

import json

from app.api.v1.routes.events import event_frames
from app.core.events import EventBus

from .conftest import ApiClient


def parse(frame: str) -> tuple[str, dict[str, object]]:
    lines = frame.splitlines()
    event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
    data = next((line.removeprefix("data: ") for line in lines if line.startswith("data: ")), "{}")
    return event, json.loads(data)


class Disconnect:
    """Reports the client as gone after a fixed number of checks."""

    def __init__(self, after: int) -> None:
        self._remaining = after

    async def __call__(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


class TestAuthentication:
    def test_refuses_an_anonymous_subscriber(self, client: ApiClient) -> None:
        response = client.raw.get("/api/v1/events/stream")
        assert response.status_code == 401

    def test_refuses_a_bad_token(self, client: ApiClient) -> None:
        response = client.raw.get("/api/v1/events/stream?token=not-a-jwt")
        assert response.status_code == 401

    def test_the_error_explains_why_a_query_token_is_needed(self, client: ApiClient) -> None:
        body = client.raw.get("/api/v1/events/stream").json()
        assert "EventSource cannot send an Authorization header" in body["detail"]

    def test_a_user_owing_a_password_change_cannot_subscribe(self, client: ApiClient) -> None:
        """The forced rotation must not be sidesteppable by opening a stream instead."""
        client.login()
        response = client.raw.get(f"/api/v1/events/stream?token={client.access_token}")
        assert response.status_code == 401


class TestStream:
    async def test_opens_with_a_named_frame(self) -> None:
        """A silent stream and a broken one look identical without this."""
        bus = EventBus()
        frames = [frame async for frame in event_frames(bus, is_disconnected=Disconnect(0))]

        assert parse(frames[0]) == ("stream.open", {})

    async def test_delivers_published_events_in_order(self) -> None:
        bus = EventBus()
        collected: list[str] = []

        generator = event_frames(bus, is_disconnected=Disconnect(2))
        collected.append(await anext(generator))  # stream.open

        bus.publish("backup.progress", backup_id=7, percent=42.0)
        bus.publish("run.state", run_id=7, status="success")
        collected.append(await anext(generator))
        collected.append(await anext(generator))
        await generator.aclose()

        assert [parse(frame)[0] for frame in collected[1:]] == ["backup.progress", "run.state"]
        _, payload = parse(collected[1])
        assert (payload["backup_id"], payload["percent"]) == (7, 42.0)
        assert "ts" in payload

    async def test_sends_a_heartbeat_when_nothing_is_happening(self) -> None:
        """Without it an idle proxy closes the connection and the UI goes quiet."""
        bus = EventBus()
        generator = event_frames(bus, is_disconnected=Disconnect(1), heartbeat_seconds=0.01)
        await anext(generator)  # stream.open
        heartbeat = await anext(generator)
        await generator.aclose()

        assert heartbeat == ": keep-alive\n\n"

    async def test_unsubscribes_when_the_client_disconnects(self) -> None:
        bus = EventBus()
        generator = event_frames(bus, is_disconnected=Disconnect(0))
        await anext(generator)
        assert bus.subscriber_count == 1

        # The generator ends as soon as the disconnect check reports True.
        assert [frame async for frame in generator] == []
        assert bus.subscriber_count == 0

    async def test_a_payload_survives_json_encoding(self) -> None:
        """Events carry datetimes; a serialisation failure here would kill the stream."""
        from datetime import UTC, datetime

        bus = EventBus()
        generator = event_frames(bus, is_disconnected=Disconnect(1))
        await anext(generator)

        bus.publish("run.state", run_id=1, finished_at=datetime.now(UTC))
        _, payload = parse(await anext(generator))
        await generator.aclose()

        assert isinstance(payload["finished_at"], str)
