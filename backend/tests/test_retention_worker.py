"""Retention worker serialization, correlation, and notification coalescing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import AgentError
from app.core.logging import get_correlation_id
from app.core.retention_guard import RetentionGuard
from app.db.models.backup import BackupHistory, BackupJob, BackupRun, RetentionEvent
from app.db.session import Database
from app.schemas.agent import AgentDeletedArtifact
from app.schemas.backup import RunOptions
from app.schemas.enums import BackupMode, Compression, RunStatus, TriggerType
from app.schemas.retention import RetentionResponse
from app.workers.retention_worker import RetentionWorker

from .conftest import SECRET_KEY
from .test_retention_service import FakeRetentionAgent, add_rows, backup, configure


class LaneAgent(FakeRetentionAgent):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0
        self.correlations: list[str | None] = []

    async def delete_backup(self, filename: str) -> AgentDeletedArtifact:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.correlations.append(get_correlation_id())
        await asyncio.sleep(0)
        try:
            return await super().delete_backup(filename)
        finally:
            self.active -= 1


async def event_count(database: Database) -> int:
    async with database.session_factory() as session:
        value = await session.scalar(select(func.count(RetentionEvent.id)))
    return int(value or 0)


async def wait_for_events(database: Database, minimum: int) -> None:
    for _ in range(100):
        if await event_count(database) >= minimum:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"retention worker did not record {minimum} events")


async def wait_for_local_delete(database: Database, backup_id: int) -> None:
    for _ in range(100):
        async with database.session_factory() as session:
            row = await session.get(BackupHistory, backup_id)
            if row is not None and row.local_deleted_at is not None:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"backup #{backup_id} was not deleted by the retry")


class FirstPassFailsWorker(RetentionWorker):
    def __init__(
        self,
        *,
        fail_backup_id: int,
        database: Database,
        agent: FakeRetentionAgent,
        secret_box: SecretBox,
        retry_delays: tuple[float, ...],
    ) -> None:
        super().__init__(
            database=database,
            agent=agent,
            secret_box=secret_box,
            retry_delays=retry_delays,
        )
        self._fail_backup_id = fail_backup_id
        self._failed_once = False

    async def _apply_one(self, backup_id: int | None) -> RetentionResponse:
        if backup_id == self._fail_backup_id and not self._failed_once:
            self._failed_once = True
            raise RuntimeError("synthetic first-guest failure")
        return await super()._apply_one(backup_id)


class TransientDeleteAgent(FakeRetentionAgent):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def delete_backup(self, filename: str) -> AgentDeletedArtifact:
        self.local_calls.append(filename)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise AgentError("transient delete failure", agent_status=503)
        return AgentDeletedArtifact(
            filename=filename,
            deleted=[f"/dump/{filename}"],
            freed_bytes=100,
        )


class ObservedRetentionGuard(RetentionGuard):
    """Expose a contender without weakening the production locking behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self.second_attempted = asyncio.Event()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        self.attempts += 1
        if self.attempts >= 2:
            self.second_attempted.set()
        async with super().hold():
            yield


async def test_direct_passes_are_serial_and_receive_fresh_correlations(
    settings: Settings,
    prepared_database: None,
) -> None:
    del prepared_database
    database = Database(settings)
    try:
        await configure(database.session_factory, keep_local=1, keep_remote=0)
        first = [backup(1, vmid=101), backup(2, vmid=101)]
        second = [backup(3, vmid=202), backup(4, vmid=202)]
        await add_rows(database.session_factory, [*first, *second])
        agent = LaneAgent()
        worker = RetentionWorker(
            database=database,
            agent=agent,
            secret_box=SecretBox(SECRET_KEY),
        )

        await asyncio.gather(
            worker.run_once(backup_id=first[-1].id),
            worker.run_once(backup_id=second[-1].id),
        )

        assert agent.max_active == 1
        assert len(agent.correlations) == 2
        assert all(agent.correlations)
        assert agent.correlations[0] != agent.correlations[1]
    finally:
        await database.dispose()


async def test_shared_guard_commits_pin_before_retention_revalidates(
    settings: Settings,
    prepared_database: None,
) -> None:
    del prepared_database
    database = Database(settings)
    release_pin = asyncio.Event()
    pin_task: asyncio.Task[None] | None = None
    retention_task: asyncio.Task[RetentionResponse] | None = None
    try:
        await configure(database.session_factory, keep_local=1, keep_remote=0)
        rows = [backup(11), backup(12)]
        await add_rows(database.session_factory, rows)
        agent = FakeRetentionAgent()
        guard = ObservedRetentionGuard()
        worker = RetentionWorker(
            database=database,
            agent=agent,
            secret_box=SecretBox(SECRET_KEY),
            guard=guard,
        )
        pin_entered = asyncio.Event()

        async def pin_candidate() -> None:
            async with (
                database.session_factory() as session,
                guard.transaction(session),
            ):
                candidate = await session.get(BackupHistory, rows[0].id)
                assert candidate is not None
                candidate.retention_locked = True
                pin_entered.set()
                await release_pin.wait()

        pin_task = asyncio.create_task(pin_candidate())
        await asyncio.wait_for(pin_entered.wait(), timeout=1)

        retention_task = asyncio.create_task(worker.run_once(backup_id=rows[-1].id))
        await asyncio.wait_for(guard.second_attempted.wait(), timeout=1)

        assert not retention_task.done()
        assert agent.local_calls == []

        release_pin.set()
        await pin_task
        response = await retention_task

        candidate_decision = next(item for item in response.items if item.backup_id == rows[0].id)
        assert candidate_decision.reason == "retention_locked"
        assert not candidate_decision.deleted
        assert agent.local_calls == []

        async with database.session_factory() as session:
            stored = await session.get(BackupHistory, rows[0].id)
        assert stored is not None
        assert stored.retention_locked
        assert stored.local_deleted_at is None
    finally:
        release_pin.set()
        for task in (pin_task, retention_task):
            if task is not None and not task.done():
                task.cancel()
        if pin_task is not None:
            await asyncio.gather(pin_task, return_exceptions=True)
        if retention_task is not None:
            await asyncio.gather(retention_task, return_exceptions=True)
        await database.dispose()


async def test_startup_sweep_and_post_commit_notifications_are_coalesced(
    settings: Settings,
    prepared_database: None,
) -> None:
    del prepared_database
    database = Database(settings)
    worker: RetentionWorker | None = None
    try:
        await configure(database.session_factory, keep_local=2, keep_remote=0)
        async with database.session_factory() as session:
            job = BackupJob(name="startup-frozen-retention")
            session.add(job)
            await session.flush()
            run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="startup-scheduled-run",
                options=RunOptions(
                    mode=BackupMode.SNAPSHOT,
                    compression=Compression.ZSTD,
                    storage="backup-hdd",
                    retention_source="job",
                    keep_local=5,
                    keep_remote=0,
                ).model_dump(mode="json"),
            )
            session.add(run)
            await session.flush()
            rows = [backup(index + 10, run_id=run.id) for index in range(1, 7)]
            session.add_all(rows)
            await session.commit()
        agent = FakeRetentionAgent()
        worker = RetentionWorker(
            database=database,
            agent=agent,
            secret_box=SecretBox(SECRET_KEY),
        )

        await worker.start()
        # Startup must use the scheduled job's frozen keep=5, not global keep=2.
        await wait_for_events(database, 6)

        async with database.session_factory() as session:
            worker.notify_after_commit(session, rows[-1].id)
            worker.notify_after_commit(session, rows[-1].id)
            assert await event_count(database) == 6
            await session.commit()

        # The deleted candidate is absent, so one coalesced pass writes five kept events.
        await wait_for_events(database, 11)
        await asyncio.sleep(0.05)
        assert await event_count(database) == 11
        assert agent.local_calls == [rows[0].filename]

        async with database.session_factory() as session:
            worker.notify_after_commit(session, None)
            await session.commit()
        await wait_for_events(database, 16)
        assert await event_count(database) == 16
    finally:
        if worker is not None:
            await worker.stop()
        await database.dispose()


async def test_stale_trigger_is_canonicalized_to_newest_policy(
    settings: Settings,
    prepared_database: None,
) -> None:
    del prepared_database
    database = Database(settings)
    try:
        await configure(database.session_factory, keep_local=2, keep_remote=0)
        async with database.session_factory() as session:
            job = BackupJob(name="canonical-policy")
            session.add(job)
            await session.flush()
            old_run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="old-keep-one",
                options=RunOptions(
                    mode=BackupMode.SNAPSHOT,
                    compression=Compression.ZSTD,
                    storage="backup-hdd",
                    retention_source="job",
                    keep_local=1,
                    keep_remote=0,
                ).model_dump(mode="json"),
            )
            newest_run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="new-keep-five",
                options=RunOptions(
                    mode=BackupMode.SNAPSHOT,
                    compression=Compression.ZSTD,
                    storage="backup-hdd",
                    retention_source="job",
                    keep_local=5,
                    keep_remote=0,
                ).model_dump(mode="json"),
            )
            session.add_all([old_run, newest_run])
            await session.flush()
            rows = [backup(index + 50, run_id=old_run.id) for index in range(1, 5)]
            newest = backup(55, run_id=newest_run.id)
            session.add_all([*rows, newest])
            await session.commit()

        agent = FakeRetentionAgent()
        worker = RetentionWorker(
            database=database,
            agent=agent,
            secret_box=SecretBox(SECRET_KEY),
        )
        response = await worker.run_once(backup_id=rows[-1].id)

        assert response.policy.trigger_backup_id == newest.id
        assert response.policy.keep_local == 5
        assert agent.local_calls == []
    finally:
        await database.dispose()


async def test_startup_guest_failure_does_not_block_next_guest(
    settings: Settings,
    prepared_database: None,
) -> None:
    del prepared_database
    database = Database(settings)
    worker: FirstPassFailsWorker | None = None
    try:
        await configure(database.session_factory, keep_local=1, keep_remote=0)
        first = [backup(61, vmid=101), backup(62, vmid=101)]
        second = [backup(63, vmid=202), backup(64, vmid=202)]
        await add_rows(database.session_factory, [*first, *second])
        agent = FakeRetentionAgent()
        worker = FirstPassFailsWorker(
            fail_backup_id=first[-1].id,
            database=database,
            agent=agent,
            secret_box=SecretBox(SECRET_KEY),
            retry_delays=(10.0,),
        )

        responses = await worker.run_startup_sweep()

        assert len(responses) == 1
        assert responses[0].policy.trigger_backup_id == second[-1].id
        assert agent.local_calls == [second[0].filename]
        assert worker.last_error is not None
        assert "synthetic first-guest failure" in worker.last_error
        async with database.session_factory() as session:
            first_oldest = await session.get(BackupHistory, first[0].id)
        assert first_oldest is not None and first_oldest.local_deleted_at is None
    finally:
        if worker is not None:
            await worker.stop()
        await database.dispose()


async def test_transient_delete_failure_is_retried_and_health_recovers(
    settings: Settings,
    prepared_database: None,
) -> None:
    del prepared_database
    database = Database(settings)
    worker: RetentionWorker | None = None
    try:
        await configure(database.session_factory, keep_local=1, keep_remote=0)
        rows = [backup(71), backup(72)]
        await add_rows(database.session_factory, rows)
        agent = TransientDeleteAgent()
        worker = RetentionWorker(
            database=database,
            agent=agent,
            secret_box=SecretBox(SECRET_KEY),
            retry_delays=(0.05,),
        )

        await worker.start()
        initial_error = worker.last_error
        assert initial_error is not None
        assert "transient delete failure" in initial_error
        await wait_for_local_delete(database, rows[0].id)

        assert agent.local_calls == [rows[0].filename, rows[0].filename]
        recovered_error = worker.last_error
        assert recovered_error is None
        assert await event_count(database) == 4
    finally:
        if worker is not None:
            await worker.stop()
        await database.dispose()
