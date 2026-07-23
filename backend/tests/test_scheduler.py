"""The scheduler.

`backup_jobs` is the single source of truth; APScheduler holds a derived view rebuilt from
it. These tests pin that relationship, and the catch-up behaviour that replaces the
persistent job store ProxSync deliberately does not use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.agent_client import AgentClient
from app.clients.proxmox_client import ProxmoxClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EventBus
from app.db.models.backup import BackupJob
from app.db.session import Database
from app.repositories.backup_job_repository import SqlAlchemyBackupJobRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import RunStatus, TriggerType
from app.services.inventory_service import InventoryService
from app.services.settings_service import SettingsService
from app.workers.backup_runner import BackupRunner
from app.workers.scheduler import SchedulerWorker, job_key

from .conftest import SECRET_KEY, StubProxmoxTransport, make_guest, seed_guests


@pytest.fixture
async def database(settings: Settings, prepared_database: None) -> AsyncIterator[Database]:
    del prepared_database
    instance = Database(settings)
    yield instance
    await instance.dispose()


@pytest.fixture
async def scheduler(
    settings: Settings,
    database: Database,
    proxmox: ProxmoxClient,
    proxmox_transport: StubProxmoxTransport,
) -> AsyncIterator[SchedulerWorker]:
    proxmox_transport.set_guests()
    events = EventBus()
    secret_box = SecretBox(SECRET_KEY)
    agent = AgentClient(
        settings,
        client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=_Silent()),
    )
    runner = BackupRunner(
        database=database,
        agent=agent,
        events=events,
        secret_box=secret_box,
        settings=settings,
    )
    worker = SchedulerWorker(
        database=database,
        runner=runner,
        events=events,
        settings=settings,
        secret_box=secret_box,
        inventory_factory=lambda session: _inventory(session, proxmox, secret_box),
    )
    yield worker
    await worker.stop()
    await agent.aclose()


class _Silent(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("agent not used in these tests", request=request)


def _inventory(
    session: AsyncSession, proxmox: ProxmoxClient, secret_box: SecretBox
) -> InventoryService:
    return InventoryService(
        guests=SqlAlchemyGuestRepository(session),
        proxmox=proxmox,
        settings_service=SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=secret_box
        ),
    )


async def add_job(
    database: Database,
    *,
    name: str = "weekly",
    cron: str = "0 1 * * 0",
    enabled: bool = True,
    next_run_at: datetime | None = None,
) -> int:
    async with database.session() as session:
        job = BackupJob(
            name=name,
            enabled=enabled,
            cron_expression=cron,
            timezone="Asia/Jakarta",
            target_selector="all",
            storage="backup-hdd",
            next_run_at=next_run_at,
        )
        session.add(job)
        await session.flush()
        return job.id


class TestSync:
    async def test_registers_only_enabled_jobs(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        enabled = await add_job(database, name="enabled")
        disabled = await add_job(database, name="disabled", enabled=False)

        await scheduler.start()

        registered = {job.id for job in scheduler._scheduler.get_jobs()}  # noqa: SLF001
        assert job_key(enabled) in registered
        assert job_key(disabled) not in registered

    async def test_removes_a_job_deleted_from_the_table(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        job_id = await add_job(database)
        await scheduler.start()
        assert job_key(job_id) in {j.id for j in scheduler._scheduler.get_jobs()}  # noqa: SLF001

        async with database.session() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            await session.delete(job)

        await scheduler.sync_jobs()

        assert job_key(job_id) not in {j.id for j in scheduler._scheduler.get_jobs()}  # noqa: SLF001

    async def test_a_corrupt_cron_skips_one_job_not_all_of_them(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        """Expressions are validated on write, so this means the row was edited elsewhere."""
        good = await add_job(database, name="good")
        bad = await add_job(database, name="bad", cron="not a cron")

        await scheduler.start()

        registered = {job.id for job in scheduler._scheduler.get_jobs()}
        assert job_key(good) in registered
        assert job_key(bad) not in registered

    async def test_syncing_refreshes_next_run_at(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        job_id = await add_job(database, next_run_at=None)

        await scheduler.start()

        async with database.session() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
        assert job is not None
        assert job.next_run_at is not None
        assert job.next_run_at > datetime.now(UTC)


class TestFiring:
    async def test_firing_queues_a_run_and_advances_the_schedule(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        await seed_guests(database.session_factory, make_guest(101, name="web"))
        job_id = await add_job(database)

        await scheduler._fire(job_id)  # noqa: SLF001 - driving the trigger directly

        async with database.session() as session:
            runs = await SqlAlchemyBackupRunRepository(session).list_recent()
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)

        assert len(runs) == 1
        assert runs[0].status == RunStatus.QUEUED.value
        assert runs[0].trigger == TriggerType.SCHEDULE.value
        assert job is not None
        assert job.last_run_at is not None
        assert job.next_run_at is not None and job.next_run_at > datetime.now(UTC)

    async def test_a_schedule_resolving_to_nothing_is_reported_not_swallowed(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        job_id = await add_job(database)

        async with scheduler._events.subscribe() as queue:  # noqa: SLF001
            await scheduler._fire(job_id)  # noqa: SLF001
            event = queue.get_nowait()

        assert event.data["status"] == "not_started"
        assert "resolves to no guests" in event.data["detail"]

        async with database.session() as session:
            assert await SqlAlchemyBackupRunRepository(session).count() == 0

    async def test_firing_a_disabled_job_does_nothing(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        await seed_guests(database.session_factory, make_guest(101, name="web"))
        job_id = await add_job(database, enabled=False)

        await scheduler._fire(job_id)  # noqa: SLF001

        async with database.session() as session:
            assert await SqlAlchemyBackupRunRepository(session).count() == 0


class TestCatchUp:
    async def test_a_schedule_missed_inside_the_grace_window_runs(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        """This is what replaces APScheduler's persistent job store."""
        await seed_guests(database.session_factory, make_guest(101, name="web"))
        await add_job(database, next_run_at=datetime.now(UTC) - timedelta(minutes=5))

        await scheduler.start()

        async with database.session() as session:
            assert await SqlAlchemyBackupRunRepository(session).count() == 1

    async def test_a_schedule_missed_beyond_the_grace_window_is_skipped(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        """A weekly backup starting unattended three days late is its own kind of surprise."""
        await seed_guests(database.session_factory, make_guest(101, name="web"))
        job_id = await add_job(database, next_run_at=datetime.now(UTC) - timedelta(days=3))

        await scheduler.start()

        async with database.session() as session:
            assert await SqlAlchemyBackupRunRepository(session).count() == 0
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)

        assert job is not None
        assert job.next_run_at is not None
        assert job.next_run_at > datetime.now(UTC), "the schedule must still move forward"

    async def test_a_future_schedule_is_left_alone(
        self, scheduler: SchedulerWorker, database: Database
    ) -> None:
        await seed_guests(database.session_factory, make_guest(101, name="web"))
        await add_job(database, next_run_at=datetime.now(UTC) + timedelta(days=1))

        await scheduler.start()

        async with database.session() as session:
            assert await SqlAlchemyBackupRunRepository(session).count() == 0


class TestInventoryRefresh:
    async def test_the_refresh_job_is_registered(self, scheduler: SchedulerWorker) -> None:
        await scheduler.start()
        assert "inventory-refresh" in {job.id for job in scheduler._scheduler.get_jobs()}  # noqa: SLF001

    async def test_an_unreachable_host_does_not_stop_the_scheduler(
        self, scheduler: SchedulerWorker, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.route("/qemu", {"data": None}, status_code=500)
        await scheduler.start()

        await scheduler._refresh_inventory()  # noqa: SLF001 - must not raise

        assert scheduler.running
