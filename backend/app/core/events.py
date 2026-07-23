"""In-process event bus for live UI updates.

Publishing must never block and never raise. A backup that failed because an idle browser
tab stopped draining its queue would be an absurd failure mode, so each subscriber gets a
**bounded** queue and a slow one loses its oldest events rather than applying backpressure to
the worker that produced them.

The bus is deliberately in-process and non-durable. Events are a *view* of state that is
already in the database; a client that misses one refetches and is correct again. Anything
that must survive a restart belongs in a table, not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_correlation_id, logger

# Event names are part of the API: docs/API.md §13 lists them and the frontend subscribes by
# name. They are grouped by resource so a client can filter on a prefix.
EVENT_RUN_STATE = "run.state"
EVENT_BACKUP_STATE = "backup.state"
EVENT_BACKUP_PROGRESS = "backup.progress"
EVENT_JOB_STATE = "job.state"
EVENT_INVENTORY_UPDATED = "inventory.updated"
EVENT_STORAGE_UPDATE = "storage.update"
EVENT_RESTORE_STATE = "restore.state"
EVENT_RESTORE_PROGRESS = "restore.progress"
EVENT_NOTIFICATION_STATE = "notification.state"


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: dict[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = field(default_factory=get_correlation_id)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ts": self.ts.isoformat(), **self.data}
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        return payload


class EventBus:
    """Fan-out to every live subscriber.

    Not thread-safe by design: everything in this application runs on one event loop, and
    adding a lock would imply a concurrency model that does not exist.
    """

    def __init__(self, *, queue_size: int = 200) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped_events(self) -> int:
        """Events discarded because a subscriber was not keeping up. Surfaced in /health."""
        return self._dropped

    def publish(self, event_type: str, /, **data: Any) -> Event:
        event = Event(type=event_type, data=data)
        for queue in self._subscribers:
            self._offer(queue, event)
        return event

    def _offer(self, queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest: a live progress feed is more useful than a stale one, and the
            # client's next refetch reconciles whatever it missed.
            self._dropped += 1
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - racy drain
                logger.debug("event_dropped", event_type=event.type)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        logger.debug("event_subscriber_added", subscribers=len(self._subscribers))
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            logger.debug("event_subscriber_removed", subscribers=len(self._subscribers))
