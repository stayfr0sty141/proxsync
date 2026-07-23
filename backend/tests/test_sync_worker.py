"""The upload queue worker.

The interesting behaviour is what happens when a transfer *fails*: how many times it is
retried, how long it waits, and what the backup row says while it waits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EventBus
from app.db.models.backup import BackupHistory, SyncTask
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.sync_task_repository import SqlAlchemySyncTaskRepository
from app.schemas.enums import (
    BackupStatus,
    GuestType,
    SettingsSection,
    SyncDirection,
    SyncStatus,
    UploadStatus,
)
from app.services.settings_service import SettingsService
from app.workers.sync_worker import SyncWorker

from .conftest import SECRET_KEY, RecordedRequest

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"


class ScriptedSyncTransport(httpx.AsyncBaseTransport):
    """An agent that accepts uploads and walks each task through a scripted state sequence."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.states: list[str] = ["success"]
        self.accept = True
        self.accept_error = {"detail": "rclone is not installed on this host"}
        self.error = "googleapi: Error 403: quota exceeded"
        self._remaining: dict[str, list[str]] = {}
        self._counter = 0

    @property
    def uploads(self) -> list[dict[str, Any]]:
        return [
            request.json_body
            for request in self.requests
            if request.url.path.endswith("/sync/upload")
        ]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(RecordedRequest(request))
        path = request.url.path

        if path.endswith("/sync/upload"):
            if not self.accept:
                return httpx.Response(400, json=self.accept_error)
            self._counter += 1
            task_id = f"rclone-{self._counter}"
            self._remaining[task_id] = list(self.states)
            return httpx.Response(
                202,
                json={
                    "task_id": task_id,
                    "kind": "upload",
                    "state": "queued",
                    "created_at": "2026-07-26T01:20:00Z",
                },
            )

        if "/task/" in path:
            task_id = path.rsplit("/", 1)[-1]
            remaining = self._remaining.get(task_id)
            if remaining is None:
                return httpx.Response(404, json={"detail": "unknown task"})
            state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return httpx.Response(200, json=self._render(task_id, state))

        return httpx.Response(404, json={"detail": f"unhandled {path}"})

    def _render(self, task_id: str, state: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task_id,
            "kind": "upload",
            "state": state,
            "created_at": "2026-07-26T01:20:00Z",
            "started_at": "2026-07-26T01:20:01Z",
            "progress": {},
            "result": {},
            "meta": {},
        }
        if state == "running":
            payload["progress"] = {
                "percent": 50.0,
                "bytes_done": 2048,
                "bytes_total": 4096,
                "rate_bps": 1024,
            }
        if state == "success":
            payload["finished_at"] = "2026-07-26T01:25:00Z"
            payload["duration_seconds"] = 300.0
            payload["result"] = {
                "bytes_transferred": 4096,
                "verification": {
                    "outcome": "match",
                    "verified": True,
                    "remote_md5": "a" * 32,
                    "remote_size_bytes": 4096,
                },
            }
        if state == "failed":
            payload["error"] = self.error
        return payload


@pytest.fixture
def scripted() -> ScriptedSyncTransport:
    return ScriptedSyncTransport()


@pytest.fixture
def agent(settings: Settings, scripted: ScriptedSyncTransport) -> AgentClient:
    return AgentClient(
        settings, client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=scripted)
    )


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
def worker(
    settings: Settings, database: Database, agent: AgentClient, events: EventBus
) -> SyncWorker:
    fast = settings.model_copy(
        update={
            "sync_idle_poll_seconds": 0.01,
            "sync_retry_base_seconds": 30,
            "sync_retry_max_seconds": 3600,
            "agent_unreachable_grace_seconds": 1,
        }
    )
    instance = SyncWorker(
        database=database,
        agent=agent,
        events=events,
        secret_box=SecretBox(SECRET_KEY),
        settings=fast,
    )
    instance._poll_interval = _instant  # type: ignore[method-assign]  # noqa: SLF001
    return instance


async def _instant() -> float:
    return 0.001


async def enable_gdrive(database: Database, **overrides: Any) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        await service.update_section(
            SettingsSection.GDRIVE,
            {"enabled": True, "remote_name": "gdrive", "folder": "proxsync/dump", **overrides},
        )
        # Keep the worker's polling instant; the schema floor is one second.
        await service.update_section(SettingsSection.AGENT, {"poll_interval_seconds": 1})


async def seed(
    database: Database, *, max_attempts: int = 3, filename: str = ARCHIVE
) -> tuple[int, int]:
    """A successful backup awaiting upload, plus its queued transfer.

    The filename is a parameter because `(storage, filename)` is unique — two backups of the
    same guest cannot share an artifact name, and neither can two fixtures.
    """
    async with database.session() as session:
        record = BackupHistory(
            run_id=None,
            guest_id=None,
            vmid=101,
            guest_type=GuestType.VM.value,
            guest_name="web",
            node="pve",
            storage="backup-hdd",
            filename=filename,
            size_bytes=4096,
            status=BackupStatus.SUCCESS.value,
            upload_status=UploadStatus.PENDING.value,
            started_at=datetime.now(UTC),
        )
        session.add(record)
        await session.flush()

        task = await SqlAlchemySyncTaskRepository(session).create(
            backup_id=record.id,
            direction=SyncDirection.UPLOAD,
            remote_name="gdrive",
            remote_path="proxsync/dump",
            max_attempts=max_attempts,
            correlation_id="test-correlation",
        )
        return record.id, task.id


async def read_task(database: Database, task_id: int) -> SyncTask:
    async with database.session() as session:
        task = await SqlAlchemySyncTaskRepository(session).get(task_id)
    assert task is not None
    return task


async def read_backup(database: Database, backup_id: int) -> BackupHistory:
    async with database.session() as session:
        record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
    assert record is not None
    return record


async def run_once(worker: SyncWorker) -> int | None:
    task_id = await worker._claim_next()  # noqa: SLF001 - driving one cycle deterministically
    if task_id is not None:
        await worker._execute(task_id)  # noqa: SLF001
    return task_id


class TestSuccessfulTransfer:
    async def test_marks_the_backup_uploaded(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        backup_id, task_id = await seed(database)
        scripted.states = ["running", "success"]

        await run_once(worker)

        task = await read_task(database, task_id)
        assert task.status == SyncStatus.SUCCESS.value
        assert task.agent_task_id == "rclone-1"
        assert task.finished_at is not None

        record = await read_backup(database, backup_id)
        assert record.upload_status == UploadStatus.UPLOADED.value
        assert record.uploaded_at is not None
        assert record.remote_path == "gdrive:proxsync/dump"
        assert record.remote_checksum == "a" * 32

    async def test_the_agent_is_asked_to_verify_when_configured(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database, verify_after_upload=True)
        await seed(database)

        await run_once(worker)

        [upload] = scripted.uploads
        assert upload["verify_after"] is True
        assert upload["filename"] == ARCHIVE
        assert upload["remote"] == "gdrive"
        assert upload["remote_path"] == "proxsync/dump"

    async def test_bandwidth_and_transfers_come_from_settings(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database, bandwidth_limit_kbps=4096, transfers=8)
        await seed(database)

        await run_once(worker)

        [upload] = scripted.uploads
        assert upload["bwlimit_kbps"] == 4096
        assert upload["transfers"] == 8

    async def test_progress_is_persisted_so_a_resume_does_not_restart_at_zero(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        _, task_id = await seed(database)
        scripted.states = ["running", "success"]

        await run_once(worker)

        task = await read_task(database, task_id)
        assert task.bytes_transferred == 4096


class TestRetries:
    async def test_a_failure_schedules_a_retry_rather_than_giving_up(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        backup_id, task_id = await seed(database, max_attempts=3)
        scripted.states = ["failed"]

        await run_once(worker)

        task = await read_task(database, task_id)
        assert task.status == SyncStatus.QUEUED.value
        assert task.attempt == 1
        assert task.next_retry_at is not None
        assert task.next_retry_at > datetime.now(UTC)
        assert "quota exceeded" in (task.error_message or "")

        # Still 'pending', not 'failed': a retry is scheduled and the tile should say so.
        record = await read_backup(database, backup_id)
        assert record.upload_status == UploadStatus.PENDING.value

    async def test_a_transfer_waiting_on_backoff_is_not_claimed(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        await seed(database)
        scripted.states = ["failed"]

        await run_once(worker)

        assert await worker._claim_next() is None, "the backoff must actually hold"  # noqa: SLF001

    async def test_the_retry_runs_once_the_delay_has_passed(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        backup_id, task_id = await seed(database)
        scripted.states = ["failed"]
        await run_once(worker)

        async with database.session() as session:
            task = await SqlAlchemySyncTaskRepository(session).get(task_id)
            assert task is not None
            task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)

        scripted.states = ["success"]
        await run_once(worker)

        task = await read_task(database, task_id)
        assert task.status == SyncStatus.SUCCESS.value
        assert task.attempt == 2
        assert (await read_backup(database, backup_id)).upload_status == UploadStatus.UPLOADED.value

    async def test_exhausting_the_attempts_fails_the_upload(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        backup_id, task_id = await seed(database, max_attempts=2)
        scripted.states = ["failed"]

        for _ in range(2):
            async with database.session() as session:
                task = await SqlAlchemySyncTaskRepository(session).get(task_id)
                assert task is not None
                task.next_retry_at = None
            await run_once(worker)

        task = await read_task(database, task_id)
        assert task.status == SyncStatus.FAILED.value
        assert task.attempt == 2
        assert task.next_retry_at is None
        assert task.finished_at is not None

        record = await read_backup(database, backup_id)
        assert record.upload_status == UploadStatus.FAILED.value

    async def test_a_refused_request_counts_as_an_attempt(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        """A misconfigured agent must not be retried forever without the count moving."""
        await enable_gdrive(database)
        _, task_id = await seed(database)
        scripted.accept = False

        await run_once(worker)

        task = await read_task(database, task_id)
        assert task.attempt == 1
        assert "rclone is not installed" in (task.error_message or "")

    def test_backoff_grows_and_is_bounded(self, worker: SyncWorker) -> None:
        base = worker._settings.sync_retry_base_seconds  # noqa: SLF001
        ceiling = worker._settings.sync_retry_max_seconds  # noqa: SLF001

        for attempt in range(1, 12):
            delay = worker._retry_delay(attempt)  # noqa: SLF001
            assert base <= delay <= ceiling

        # Jittered, so two calls at the same attempt should not be identical every time.
        samples = {worker._retry_delay(5) for _ in range(20)}  # noqa: SLF001
        assert len(samples) > 1, "backoff must be jittered, or a quota error retries in lockstep"


class TestRecovery:
    async def test_a_transfer_left_running_is_requeued_without_burning_an_attempt(
        self, worker: SyncWorker, database: Database
    ) -> None:
        """The process died — that is not the transfer's fault."""
        await enable_gdrive(database)
        _, task_id = await seed(database)
        async with database.session() as session:
            task = await SqlAlchemySyncTaskRepository(session).get(task_id)
            assert task is not None
            task.status = SyncStatus.RUNNING.value
            task.attempt = 2

        await worker.recover()

        task = await read_task(database, task_id)
        assert task.status == SyncStatus.QUEUED.value
        assert task.attempt == 1
        assert task.next_retry_at is None

    async def test_a_transfer_whose_backup_vanished_fails_cleanly(
        self, worker: SyncWorker, database: Database, scripted: ScriptedSyncTransport
    ) -> None:
        await enable_gdrive(database)
        async with database.session() as session:
            task = await SqlAlchemySyncTaskRepository(session).create(
                backup_id=None,
                direction=SyncDirection.UPLOAD,
                remote_name="gdrive",
                remote_path="proxsync/dump",
                max_attempts=3,
                correlation_id=None,
            )
            task_id = task.id

        await run_once(worker)

        assert (await read_task(database, task_id)).status == SyncStatus.FAILED.value
        assert scripted.uploads == []


class TestQueueOrdering:
    async def test_claims_the_oldest_ready_transfer_first(
        self, worker: SyncWorker, database: Database
    ) -> None:
        """Newest-first would starve the transfer that has already failed and waited longest."""
        await enable_gdrive(database)
        _, first = await seed(database)
        await seed(database, filename="vzdump-qemu-102-2026_07_26-02_00_00.vma.zst")

        assert await worker._claim_next() == first  # noqa: SLF001

    async def test_pending_backups_are_queued_automatically(
        self, worker: SyncWorker, database: Database
    ) -> None:
        """A backup that finished while the sync worker was stopped still gets uploaded."""
        await enable_gdrive(database)
        async with database.session() as session:
            record = BackupHistory(
                run_id=None,
                guest_id=None,
                vmid=102,
                guest_type=GuestType.VM.value,
                guest_name="db",
                node="pve",
                storage="backup-hdd",
                filename="vzdump-qemu-102-2026_07_26-02_00_00.vma.zst",
                size_bytes=2048,
                status=BackupStatus.SUCCESS.value,
                upload_status=UploadStatus.PENDING.value,
                started_at=datetime.now(UTC),
            )
            session.add(record)

        await worker._enqueue_pending()  # noqa: SLF001

        async with database.session() as session:
            assert await SqlAlchemySyncTaskRepository(session).count() == 1


class TestEvents:
    async def test_publishes_state_and_progress(
        self,
        worker: SyncWorker,
        database: Database,
        events: EventBus,
        scripted: ScriptedSyncTransport,
    ) -> None:
        await enable_gdrive(database)
        await seed(database)
        scripted.states = ["running", "success"]

        async with events.subscribe() as queue:
            await run_once(worker)
            frames = [queue.get_nowait() for _ in range(queue.qsize())]

        kinds = [frame.type for frame in frames]
        assert "sync.state" in kinds
        assert "sync.progress" in kinds
