"""The event bus.

The property that matters most: publishing must never block or raise, whatever the
subscribers are doing. A backup that stalled because a browser tab stopped reading would be
an absurd failure mode, so it is asserted directly.
"""

from __future__ import annotations

import asyncio

from app.core.events import EVENT_BACKUP_PROGRESS, EventBus


class TestFanOut:
    async def test_every_subscriber_receives_an_event(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as first, bus.subscribe() as second:
            bus.publish(EVENT_BACKUP_PROGRESS, backup_id=1, percent=42.0)

            for queue in (first, second):
                event = await asyncio.wait_for(queue.get(), timeout=1)
                assert event.type == EVENT_BACKUP_PROGRESS
                assert event.data["percent"] == 42.0

    async def test_payload_carries_a_timestamp(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as queue:
            bus.publish("run.state", run_id=7)
            payload = (await queue.get()).to_payload()
        assert payload["run_id"] == 7
        assert "ts" in payload

    async def test_unsubscribed_queues_stop_receiving(self) -> None:
        bus = EventBus()
        async with bus.subscribe() as queue:
            assert bus.subscriber_count == 1
        assert bus.subscriber_count == 0

        bus.publish("run.state", run_id=1)
        assert queue.empty()

    def test_publishing_with_no_subscribers_is_a_no_op(self) -> None:
        bus = EventBus()
        event = bus.publish("run.state", run_id=1)
        assert event.type == "run.state"
        assert bus.subscriber_count == 0


class TestBackpressure:
    async def test_a_full_queue_drops_the_oldest_event_not_the_newest(self) -> None:
        bus = EventBus(queue_size=3)
        async with bus.subscribe() as queue:
            for index in range(5):
                bus.publish(EVENT_BACKUP_PROGRESS, percent=float(index))

            received = [queue.get_nowait().data["percent"] for _ in range(3)]

        # The two oldest were discarded: a live progress feed is worth more than a stale one.
        assert received == [2.0, 3.0, 4.0]
        assert bus.dropped_events == 2

    async def test_a_stalled_subscriber_does_not_block_the_publisher(self) -> None:
        bus = EventBus(queue_size=1)
        async with bus.subscribe() as slow, bus.subscribe() as fast:
            del slow  # never drained
            for index in range(50):
                bus.publish(EVENT_BACKUP_PROGRESS, percent=float(index))
                # The fast subscriber keeps up and is unaffected by the slow one.
                fast.get_nowait()

        assert bus.dropped_events > 0
