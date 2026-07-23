"""The storage sampler's failure, timing and threshold semantics."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EVENT_STORAGE_UPDATE, EventBus
from app.db.session import Database
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.storage_snapshot_repository import (
    SqlAlchemyStorageSnapshotRepository,
    StorageSnapshotRecord,
)
from app.schemas.agent import AgentRemoteQuota, AgentStorageStatus
from app.schemas.enums import SettingsSection
from app.services.settings_service import SettingsService
from app.workers.storage_sampler import StorageSampler
from tests.conftest import SECRET_KEY

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


@pytest.fixture
async def database(settings: Settings, prepared_database: None) -> AsyncIterator[Database]:
    del prepared_database
    value = Database(settings)
    yield value
    await value.dispose()


class ScriptedStorageAgent:
    def __init__(self, *, free: list[int] | None = None) -> None:
        self.free = free or [400]
        self.local_failures = 0
        self.remote_error: Exception | None = None
        self.local_calls = 0
        self.remote_calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.block = False

    async def storage_status(self) -> AgentStorageStatus:
        self.local_calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.block:
                await asyncio.Future()
            if self.local_failures:
                self.local_failures -= 1
                raise RuntimeError("agent offline")
            index = min(self.local_calls - 1, len(self.free) - 1)
            available = self.free[index]
            return AgentStorageStatus.model_validate(
                {
                    "dump_root": {
                        "path": "/mnt/backup-hdd/dump",
                        "total_bytes": 1_000,
                        "used_bytes": 1_000 - available,
                        "free_bytes": available,
                        "used_percent": (1_000 - available) / 10,
                    },
                    "storages": [],
                    "artifact_count": 3,
                    "artifact_bytes": 500,
                }
            )
        finally:
            self.active -= 1

    async def remote_quota(self, *, remote: str) -> AgentRemoteQuota:
        assert remote == "gdrive"
        self.remote_calls += 1
        if self.remote_error is not None:
            raise self.remote_error
        return AgentRemoteQuota(
            remote=remote,
            total_bytes=10_000,
            used_bytes=2_000,
            free_bytes=8_000,
            used_percent=20,
        )


def sampler(
    database: Database,
    settings: Settings,
    agent: ScriptedStorageAgent,
    events: EventBus | None = None,
) -> StorageSampler:
    return StorageSampler(
        database=database,
        agent=agent,
        events=events or EventBus(),
        settings=settings,
        secret_box=SecretBox(SECRET_KEY),
    )


async def snapshots(database: Database) -> list[StorageSnapshotRecord]:
    async with database.session() as session:
        return await SqlAlchemyStorageSnapshotRepository(session).since(
            cutoff=NOW - timedelta(days=1), until=NOW + timedelta(days=1)
        )


async def enable_remote(database: Database) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.update_section(SettingsSection.GDRIVE, {"enabled": True})


class TestSampling:
    async def test_local_failure_writes_no_fake_zero_snapshot(
        self, database: Database, settings: Settings
    ) -> None:
        agent = ScriptedStorageAgent()
        agent.local_failures = 1
        worker = sampler(database, settings, agent)

        assert await worker.sample_once(now=NOW) is None
        assert await snapshots(database) == []
        assert worker.last_error == "agent offline"

    async def test_remote_failure_still_commits_the_local_half(
        self, database: Database, settings: Settings
    ) -> None:
        await enable_remote(database)
        agent = ScriptedStorageAgent()
        agent.remote_error = RuntimeError("rclone about failed")
        worker = sampler(database, settings, agent)

        saved = await worker.sample_once(now=NOW)

        assert saved is not None
        assert saved.local_free_bytes == 400
        assert saved.remote_used_bytes is None
        assert saved.remote_quota_bytes is None
        assert worker.last_error == "rclone about failed"

    async def test_event_is_published_after_the_row_is_committed(
        self, database: Database, settings: Settings
    ) -> None:
        events = EventBus()
        worker = sampler(database, settings, ScriptedStorageAgent(), events)

        async with events.subscribe() as queue:
            saved = await worker.sample_once(now=NOW)
            event = await asyncio.wait_for(queue.get(), timeout=1)

            async with database.session() as session:
                visible = await SqlAlchemyStorageSnapshotRepository(session).latest()

        assert saved is not None and visible is not None
        assert event.type == EVENT_STORAGE_UPDATE
        assert event.data["snapshot_id"] == visible.id == saved.id

    async def test_only_upward_threshold_transitions_alert(
        self, database: Database, settings: Settings
    ) -> None:
        # healthy, warning, steady warning, critical, lower warning, healthy, warning again
        agent = ScriptedStorageAgent(free=[200, 140, 130, 40, 100, 200, 140])
        events = EventBus()
        worker = sampler(database, settings, agent, events)
        alerts: list[bool] = []
        severities: list[str] = []

        async with events.subscribe() as queue:
            for minute in range(7):
                await worker.sample_once(now=NOW + timedelta(minutes=minute))
                event = await asyncio.wait_for(queue.get(), timeout=1)
                alerts.append(bool(event.data["alert"]))
                severities.append(str(event.data["severity"]))

        assert severities == [
            "healthy",
            "warning",
            "warning",
            "critical",
            "warning",
            "healthy",
            "warning",
        ]
        assert alerts == [False, True, False, True, False, False, True]


class TestLifecycle:
    async def test_samples_immediately_repeats_without_overlap_and_survives_failure(
        self, database: Database, settings: Settings
    ) -> None:
        settings.storage_sample_interval_seconds = 0.01
        agent = ScriptedStorageAgent()
        agent.local_failures = 1
        worker = sampler(database, settings, agent)

        await worker.start()
        deadline = asyncio.get_running_loop().time() + 1
        while len(await snapshots(database)) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        assert agent.local_calls >= 2
        assert agent.max_active == 1
        assert worker.running
        await worker.stop()
        assert not worker.running

    async def test_stop_cancels_a_stuck_agent_request_promptly(
        self, database: Database, settings: Settings
    ) -> None:
        agent = ScriptedStorageAgent()
        agent.block = True
        worker = sampler(database, settings, agent)

        await worker.start()
        await asyncio.wait_for(agent.entered.wait(), timeout=1)
        await asyncio.wait_for(worker.stop(), timeout=0.2)

        assert not worker.running
        assert await snapshots(database) == []
