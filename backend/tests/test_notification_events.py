"""Which events actually reach the outbox, and how many times.

The acceptance criterion for M7 is "every event fires exactly once with correct content", so
these tests drive the real workers and then read the `notifications` table. They are
deliberately end-to-end within the dashboard: a template that renders and a worker that never
calls it would pass a unit test and tell an operator nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EventBus
from app.db.models.backup import BackupHistory, RestoreHistory, SyncTask
from app.db.models.system import Notification
from app.db.session import Database
from app.repositories.notification_repository import SqlAlchemyNotificationRepository
from app.repositories.retention_repository import SqlAlchemyRetentionRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestType,
    NotificationEvent,
    NotificationStatus,
    RestoreSource,
    RestoreStatus,
    SettingsSection,
    SyncDirection,
    SyncStatus,
    UploadStatus,
)
from app.services.notification_service import NotificationGateway, NotificationService
from app.services.retention_service import RetentionService
from app.services.settings_service import SettingsService
from app.workers.backup_runner import BackupRunner
from app.workers.restore_worker import RestoreWorker
from app.workers.storage_sampler import StorageSampler
from app.workers.sync_worker import SyncWorker

from .conftest import SECRET_KEY, make_guest
from .test_backup_runner import ScriptedAgentTransport, make_run
from .test_retention_service import FakeRetentionAgent
from .test_storage_worker import ScriptedStorageAgent

BOT_TOKEN = "999:test-bot-token"  # noqa: S105 - a fixture, not a credential


@pytest.fixture
async def database(settings: Settings, prepared_database: None) -> AsyncIterator[Database]:
    del prepared_database
    instance = Database(settings)
    yield instance
    await instance.dispose()


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def scripted() -> ScriptedAgentTransport:
    return ScriptedAgentTransport()


@pytest.fixture
def agent(settings: Settings, scripted: ScriptedAgentTransport) -> AgentClient:
    return AgentClient(
        settings, client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=scripted)
    )


@pytest.fixture
def gateway() -> NotificationGateway:
    """No doorbell: these tests assert what is written, not that delivery was woken."""
    return NotificationGateway(
        factory=lambda session: NotificationService(
            notifications=SqlAlchemyNotificationRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
            ),
            max_attempts=5,
            suppress_window_seconds=300,
        )
    )


@pytest.fixture(autouse=True)
async def telegram_enabled(database: Database) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        await service.update_section(
            SettingsSection.TELEGRAM,
            {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": "-100999"},
        )


async def outbox(database: Database, event: NotificationEvent | None = None) -> list[Notification]:
    async with database.session() as session:
        return await SqlAlchemyNotificationRepository(session).search(event=event, limit=100)


def text_of(record: Notification) -> str:
    return str((record.payload or {}).get("text", ""))


# ---- backup runs -------------------------------------------------------------


@pytest.fixture
def runner_with_notifications(
    settings: Settings,
    database: Database,
    agent: AgentClient,
    events: EventBus,
    gateway: NotificationGateway,
) -> BackupRunner:
    fast = settings.model_copy(
        update={"run_idle_poll_seconds": 0.01, "agent_unreachable_grace_seconds": 1}
    )
    instance = BackupRunner(
        database=database,
        agent=agent,
        events=events,
        secret_box=SecretBox(SECRET_KEY),
        settings=fast,
        notifications=gateway,
    )
    instance._poll_interval = _instant  # type: ignore[method-assign]  # noqa: SLF001
    return instance


async def _instant() -> float:
    return 0.001


async def test_a_successful_run_announces_its_start_and_its_outcome_once(
    runner_with_notifications: BackupRunner,
    database: Database,
    scripted: ScriptedAgentTransport,
) -> None:
    scripted.states = ["running", "success"]
    run_id = await make_run(database, targets=[(101, GuestType.VM), (201, GuestType.LXC)])

    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001

    events = [record.event_type for record in await outbox(database)]
    assert sorted(events) == ["backup_started", "backup_success"]

    [started] = await outbox(database, NotificationEvent.BACKUP_STARTED)
    assert "2 guest(s)" in text_of(started)
    assert "guest-101 (vm/101)" in text_of(started)

    [finished] = await outbox(database, NotificationEvent.BACKUP_SUCCESS)
    assert "2/2 guest(s)" in text_of(finished)
    assert finished.dedupe_key == f"run:{run_id}:backup_success"


async def test_a_partial_run_reports_failure_and_names_the_guests(
    runner_with_notifications: BackupRunner,
    database: Database,
    scripted: ScriptedAgentTransport,
) -> None:
    """`partial` is not a separate message: at least one guest has no backup tonight, which is
    what the operator has to act on."""
    scripted.set_states(101, ["success"])
    scripted.set_states(201, ["failed"])
    run_id = await make_run(database, targets=[(101, GuestType.VM), (201, GuestType.LXC)])

    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001

    [failed] = await outbox(database, NotificationEvent.BACKUP_FAILED)
    assert "Backup incomplete" in text_of(failed)
    assert "1/2 guest(s) succeeded" in text_of(failed)
    assert "guest-201 (lxc/201)" in text_of(failed)
    assert "vzdump failed" in text_of(failed)
    assert await outbox(database, NotificationEvent.BACKUP_SUCCESS) == []


async def test_a_run_where_everything_failed_says_so(
    runner_with_notifications: BackupRunner,
    database: Database,
    scripted: ScriptedAgentTransport,
) -> None:
    scripted.states = ["failed"]
    run_id = await make_run(database, targets=[(101, GuestType.VM)])

    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001

    [failed] = await outbox(database, NotificationEvent.BACKUP_FAILED)
    assert "Backup failed" in text_of(failed)


async def test_a_cancelled_run_is_silent_about_its_outcome(
    runner_with_notifications: BackupRunner,
    database: Database,
    scripted: ScriptedAgentTransport,
) -> None:
    """The operator who stopped it already knows. A "backup failed" alert for something they
    did themselves is the fastest way to teach them to ignore alerts."""
    scripted.states = ["running", "cancelled"]
    run_id = await make_run(database, targets=[(101, GuestType.VM)])
    runner_with_notifications._cancelled.add(run_id)  # noqa: SLF001

    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001

    events = [record.event_type for record in await outbox(database)]
    assert events == ["backup_started"]


async def test_a_resumed_run_does_not_announce_a_second_start(
    runner_with_notifications: BackupRunner,
    database: Database,
    scripted: ScriptedAgentTransport,
) -> None:
    """Restart recovery re-executes the run; the guarantee that it is announced once lives in
    the database, not in a timer."""
    scripted.states = ["success"]
    run_id = await make_run(database, targets=[(101, GuestType.VM)])

    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001
    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001

    assert len(await outbox(database, NotificationEvent.BACKUP_STARTED)) == 1
    assert len(await outbox(database, NotificationEvent.BACKUP_SUCCESS)) == 1


async def test_nothing_is_queued_while_the_channel_is_off(
    runner_with_notifications: BackupRunner,
    database: Database,
    scripted: ScriptedAgentTransport,
) -> None:
    async with database.session() as session:
        await SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        ).update_section(SettingsSection.TELEGRAM, {"enabled": False})
    scripted.states = ["success"]
    run_id = await make_run(database, targets=[(101, GuestType.VM)])

    await runner_with_notifications._execute_run(run_id)  # noqa: SLF001

    assert await outbox(database) == []


# ---- uploads -----------------------------------------------------------------


async def seed_upload(database: Database, *, attempt: int, max_attempts: int) -> int:
    async with database.session() as session:
        guest = make_guest(108, guest_type=GuestType.LXC, name="nextcloud")
        session.add(guest)
        await session.flush()
        backup = BackupHistory(
            run_id=None,
            guest_id=guest.id,
            vmid=108,
            guest_type=GuestType.LXC.value,
            guest_name="nextcloud",
            node="pve",
            storage="backup-hdd",
            filename="vzdump-lxc-108-2026_07_26-01_00_04.tar.zst",
            status=BackupStatus.SUCCESS.value,
            upload_status=UploadStatus.PENDING.value,
            mode=BackupMode.SNAPSHOT.value,
            compression=Compression.ZSTD.value,
            started_at=datetime.now(UTC),
        )
        session.add(backup)
        await session.flush()
        task = SyncTask(
            backup_id=backup.id,
            direction=SyncDirection.UPLOAD.value,
            status=SyncStatus.RUNNING.value,
            remote_name="gdrive",
            remote_path="proxsync/dump",
            attempt=attempt,
            max_attempts=max_attempts,
        )
        session.add(task)
        await session.flush()
        return task.id


@pytest.fixture
def sync_worker_with_notifications(
    settings: Settings,
    database: Database,
    agent: AgentClient,
    events: EventBus,
    gateway: NotificationGateway,
) -> SyncWorker:
    return SyncWorker(
        database=database,
        agent=agent,
        events=events,
        secret_box=SecretBox(SECRET_KEY),
        settings=settings,
        notifications=gateway,
    )


async def test_an_upload_is_announced_only_once_its_retries_are_spent(
    sync_worker_with_notifications: SyncWorker, database: Database
) -> None:
    """Alerting on every attempt would turn one Drive rate limit into a stream of warnings
    about a transfer that is still going to succeed."""
    task_id = await seed_upload(database, attempt=1, max_attempts=3)

    await sync_worker_with_notifications._record_failure(task_id, "429 rate limited")  # noqa: SLF001

    assert await outbox(database, NotificationEvent.UPLOAD_FAILED) == []


async def test_an_exhausted_upload_names_the_artifact_and_the_error(
    sync_worker_with_notifications: SyncWorker, database: Database
) -> None:
    task_id = await seed_upload(database, attempt=3, max_attempts=3)

    await sync_worker_with_notifications._record_failure(task_id, "quota exceeded")  # noqa: SLF001

    [record] = await outbox(database, NotificationEvent.UPLOAD_FAILED)
    assert "vzdump-lxc-108" in text_of(record)
    assert "lxc/108" in text_of(record)
    assert "quota exceeded" in text_of(record)
    # The reassurance matters: an operator seeing "upload failed" should not go looking for a
    # local copy that retention is about to remove — it will not, and the message says so.
    assert "retention will not delete it" in text_of(record)


# ---- restores ----------------------------------------------------------------


async def seed_restore(
    database: Database, *, status: RestoreStatus = RestoreStatus.CONFIRMED
) -> int:
    async with database.session() as session:
        guest = make_guest(101, name="web")
        session.add(guest)
        await session.flush()
        backup = BackupHistory(
            run_id=None,
            guest_id=guest.id,
            vmid=101,
            guest_type=GuestType.VM.value,
            guest_name="web",
            node="pve",
            storage="backup-hdd",
            filename="vzdump-qemu-101-2026_07_26-01_00_04.vma.zst",
            status=BackupStatus.SUCCESS.value,
            upload_status=UploadStatus.NOT_REQUIRED.value,
            mode=BackupMode.SNAPSHOT.value,
            compression=Compression.ZSTD.value,
            started_at=datetime.now(UTC),
        )
        session.add(backup)
        await session.flush()
        restore = RestoreHistory(
            backup_id=backup.id,
            source=RestoreSource.LOCAL.value,
            target_vmid=155,
            target_type=GuestType.VM.value,
            restore_mode="new_id",
            target_node="pve",
            target_storage="local-lvm",
            overwrite_existing=False,
            force_stop=False,
            start_after_restore=False,
            status=status.value,
            correlation_id="restore-correlation",
        )
        session.add(restore)
        await session.flush()
        return restore.id


@pytest.fixture
def restore_worker_with_notifications(
    settings: Settings,
    database: Database,
    agent: AgentClient,
    events: EventBus,
    gateway: NotificationGateway,
) -> RestoreWorker:
    return RestoreWorker(
        database=database,
        agent=agent,
        events=events,
        secret_box=SecretBox(SECRET_KEY),
        settings=settings,
        notifications=gateway,
    )


async def test_claiming_a_restore_announces_it(
    restore_worker_with_notifications: RestoreWorker, database: Database
) -> None:
    await seed_restore(database)

    await restore_worker_with_notifications._claim_next()  # noqa: SLF001

    [record] = await outbox(database, NotificationEvent.RESTORE_STARTED)
    assert "vzdump-qemu-101" in text_of(record)
    assert "vm 155" in text_of(record)
    assert record.restore_id is not None


async def test_an_adopted_restore_is_not_announced_twice(
    restore_worker_with_notifications: RestoreWorker, database: Database
) -> None:
    restore_id = await seed_restore(database)
    await restore_worker_with_notifications._claim_next()  # noqa: SLF001
    async with database.session() as session:
        record = await session.get(RestoreHistory, restore_id)
        assert record is not None
        record.status = RestoreStatus.CONFIRMED.value

    await restore_worker_with_notifications._claim_next()  # noqa: SLF001

    assert len(await outbox(database, NotificationEvent.RESTORE_STARTED)) == 1


async def test_a_failed_restore_is_reported_with_its_reason(
    restore_worker_with_notifications: RestoreWorker, database: Database
) -> None:
    restore_id = await seed_restore(database, status=RestoreStatus.RUNNING)

    await restore_worker_with_notifications._close(  # noqa: SLF001
        restore_id, RestoreStatus.FAILED, "storage 'local-lvm' is not active"
    )

    [record] = await outbox(database, NotificationEvent.RESTORE_FAILED)
    assert "Restore failed" in text_of(record)
    assert "local-lvm" in text_of(record)


async def test_an_interrupted_restore_says_the_outcome_is_unknown(
    restore_worker_with_notifications: RestoreWorker, database: Database
) -> None:
    """The status word carries the whole difference: "failed" means nothing changed,
    "interrupted" means nobody knows what changed."""
    restore_id = await seed_restore(database, status=RestoreStatus.RUNNING)

    await restore_worker_with_notifications._close(  # noqa: SLF001
        restore_id, RestoreStatus.INTERRUPTED, "the agent stopped answering"
    )

    [record] = await outbox(database, NotificationEvent.RESTORE_FAILED)
    assert "Restore outcome unknown" in text_of(record)


async def test_a_cancelled_restore_is_silent(
    restore_worker_with_notifications: RestoreWorker, database: Database
) -> None:
    restore_id = await seed_restore(database, status=RestoreStatus.RUNNING)

    await restore_worker_with_notifications._close(  # noqa: SLF001
        restore_id, RestoreStatus.CANCELLED, "cancelled by admin"
    )

    assert await outbox(database, NotificationEvent.RESTORE_FAILED) == []
    assert await outbox(database, NotificationEvent.RESTORE_FINISHED) == []


# ---- storage thresholds ------------------------------------------------------


def sampler_with_notifications(
    *,
    settings: Settings,
    database: Database,
    agent: ScriptedStorageAgent,
    events: EventBus,
    gateway: NotificationGateway,
) -> StorageSampler:
    return StorageSampler(
        database=database,
        agent=agent,
        events=events,
        settings=settings,
        secret_box=SecretBox(SECRET_KEY),
        notifications=gateway,
    )


async def test_crossing_into_warning_is_announced_once(
    settings: Settings, database: Database, events: EventBus, gateway: NotificationGateway
) -> None:
    """A disk that stays over the line must not alert every fifteen minutes. Only the
    transition is news, and the sampler already knows which sample is a transition."""
    # 120 free of 1000 is 88% used — over the 85% warning threshold, under the 95% critical.
    agent = ScriptedStorageAgent(free=[500, 120, 118])
    sampler = sampler_with_notifications(
        settings=settings, database=database, agent=agent, events=events, gateway=gateway
    )

    await sampler.sample_once()  # healthy
    await sampler.sample_once()  # crosses into warning
    await sampler.sample_once()  # still warning

    [record] = await outbox(database, NotificationEvent.STORAGE_THRESHOLD)
    assert "Storage warning" in text_of(record)
    assert "88.0%" in text_of(record)
    assert record.status == NotificationStatus.PENDING.value


async def test_a_healthy_disk_says_nothing(
    settings: Settings, database: Database, events: EventBus, gateway: NotificationGateway
) -> None:
    agent = ScriptedStorageAgent(free=[500])
    sampler = sampler_with_notifications(
        settings=settings, database=database, agent=agent, events=events, gateway=gateway
    )

    await sampler.sample_once()

    assert await outbox(database) == []


# ---- retention ---------------------------------------------------------------


async def test_a_retention_pass_that_deletes_reports_what_it_freed(
    database: Database, gateway: NotificationGateway
) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.update_section(SettingsSection.RETENTION, {"keep_local": 1, "keep_remote": 1})
        # Off by default — routine housekeeping is not an alert until an operator asks for it.
        await service.update_section(SettingsSection.TELEGRAM, {"notify_retention_deleted": True})

    rows = [
        retention_backup(1, started_at=datetime(2026, 7, 20, tzinfo=UTC)),
        retention_backup(2, started_at=datetime(2026, 7, 21, tzinfo=UTC)),
    ]
    async with database.session() as session:
        session.add_all(rows)
        await session.flush()
        trigger_id = rows[1].id

    agent = FakeRetentionAgent()
    async with database.session() as session:
        await RetentionService(
            repository=SqlAlchemyRetentionRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
            ),
            agent=agent,
            notifications=gateway.factory(session),
        ).apply(backup_id=trigger_id)

    [record] = await outbox(database, NotificationEvent.RETENTION_DELETED)
    assert "vm/101" in text_of(record)
    assert "1 local and 0 remote" in text_of(record)
    assert "Keeping 1 local" in text_of(record)


async def test_a_retention_pass_that_deletes_nothing_is_silent(
    database: Database, gateway: NotificationGateway
) -> None:
    """Keeping everything is the normal state of the world; a message for it is noise no one
    reads, and noise is how real alerts get ignored."""
    async with database.session() as session:
        await SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        ).update_section(SettingsSection.RETENTION, {"keep_local": 5})

    rows = [retention_backup(1, started_at=datetime(2026, 7, 20, tzinfo=UTC))]
    async with database.session() as session:
        session.add_all(rows)
        await session.flush()
        trigger_id = rows[0].id

    async with database.session() as session:
        await RetentionService(
            repository=SqlAlchemyRetentionRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
            ),
            agent=FakeRetentionAgent(),
            notifications=gateway.factory(session),
        ).apply(backup_id=trigger_id)

    assert await outbox(database, NotificationEvent.RETENTION_DELETED) == []


def retention_backup(index: int, *, started_at: datetime) -> BackupHistory:
    filename = f"vzdump-qemu-101-notify-{index}.vma.zst"
    return BackupHistory(
        run_id=None,
        guest_id=None,
        vmid=101,
        guest_type=GuestType.VM.value,
        guest_name="web",
        node="pve",
        storage="backup-hdd",
        filename=filename,
        local_path=f"/dump/{filename}",
        size_bytes=1_000,
        status=BackupStatus.SUCCESS.value,
        upload_status=UploadStatus.NOT_REQUIRED.value,
        mode=BackupMode.SNAPSHOT.value,
        compression=Compression.ZSTD.value,
        started_at=started_at,
        finished_at=started_at,
        correlation_id=f"retention-notify-{index}",
    )


# ---- the sink is not woken for nothing ---------------------------------------


async def test_the_doorbell_only_rings_for_a_message_that_needs_delivering(
    database: Database,
) -> None:
    """A suppressed row has nothing to deliver, so waking the worker for it would be pure
    churn on every storage sample."""
    rung: list[str] = []

    class Doorbell:
        def notify_after_commit(self, session: AsyncSession) -> None:
            del session
            rung.append("rung")

    gateway = NotificationGateway(
        factory=lambda session: NotificationService(
            notifications=SqlAlchemyNotificationRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
            ),
            max_attempts=5,
            suppress_window_seconds=300,
        ),
        doorbell=Doorbell(),
    )
    variables = {
        "severity": "warning",
        "previous_severity": "healthy",
        "used_bytes": 900,
        "total_bytes": 1000,
        "free_bytes": 100,
        "used_percent": 90,
    }

    async with database.session() as session:
        await gateway.emit(
            session,
            NotificationEvent.STORAGE_THRESHOLD,
            variables=variables,
            dedupe_key="storage:warning",
        )
        await gateway.emit(
            session,
            NotificationEvent.STORAGE_THRESHOLD,
            variables=variables,
            dedupe_key="storage:warning",
        )

    assert rung == ["rung"]
