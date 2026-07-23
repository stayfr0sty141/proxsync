"""The restore executor.

The interesting behaviour is what happens when the dashboard *loses track* of a restore.
`qmrestore` destroys the target and rebuilds it, so the difference between "failed" and
"unknown" is the difference between an operator retrying safely and an operator running a
second restore over a guest that is already being rewritten.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EVENT_RESTORE_STATE, EventBus
from app.core.retention_guard import RetentionGuard
from app.db.models.backup import BackupHistory, RestoreHistory
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import (
    BackupStatus,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    SettingsSection,
    UploadStatus,
)
from app.services.settings_service import SettingsService
from app.workers.restore_worker import RestoreWorker

from .conftest import SECRET_KEY, RecordedRequest

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"
CHECKSUM = "d" * 64


class ScriptedRestoreTransport(httpx.AsyncBaseTransport):
    """An agent that accepts restores and downloads and walks each task through a script."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.states: list[str] = ["success"]
        self.download_states: list[str] = ["success"]
        self.accept = True
        self.accept_error = {"detail": "VMID 151 already exists"}
        self.error = "command 'qmrestore' failed with exit code 1"
        self.warnings: list[str] = []
        self.unreachable = False
        self.unknown_tasks: set[str] = set()
        self._remaining: dict[str, list[str]] = {}
        self._kinds: dict[str, str] = {}
        self._counter = 0

    def calls(self, suffix: str) -> list[dict[str, Any]]:
        return [request.json_body for request in self.requests if request.url.path.endswith(suffix)]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(RecordedRequest(request))
        if self.unreachable:
            raise httpx.ConnectError("connection refused")

        path = request.url.path
        if path.endswith(("/restore/vm", "/restore/lxc")):
            if not self.accept:
                return httpx.Response(400, json=self.accept_error)
            return self._accept("restore", self.states)
        if path.endswith("/sync/download"):
            return self._accept("download", self.download_states)
        if "/task/" in path and path.endswith("/cancel"):
            return httpx.Response(200, json=self._render(path.split("/")[-2], "cancelled"))
        if "/task/" in path:
            task_id = path.rsplit("/", 1)[-1]
            if task_id in self.unknown_tasks:
                return httpx.Response(404, json={"detail": "unknown task"})
            remaining = self._remaining.get(task_id)
            if remaining is None:
                return httpx.Response(404, json={"detail": "unknown task"})
            state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return httpx.Response(200, json=self._render(task_id, state))

        return httpx.Response(404, json={"detail": f"unhandled {path}"})

    def _accept(self, kind: str, states: list[str]) -> httpx.Response:
        self._counter += 1
        task_id = f"{kind}-{self._counter}"
        self._remaining[task_id] = list(states)
        self._kinds[task_id] = kind
        return httpx.Response(
            202,
            json={
                "task_id": task_id,
                "kind": f"{kind}_vm",
                "state": "queued",
                "created_at": "2026-07-26T02:00:00Z",
            },
        )

    def _render(self, task_id: str, state: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task_id,
            "kind": f"{self._kinds.get(task_id, 'restore')}_vm",
            "state": state,
            "created_at": "2026-07-26T02:00:00Z",
            "started_at": "2026-07-26T02:00:01Z",
            "progress": {},
            "result": {},
            "meta": {},
        }
        if state == "running":
            payload["progress"] = {"percent": 40.0, "message": "restoring"}
        if state in {"success", "failed", "cancelled"}:
            payload["finished_at"] = "2026-07-26T02:09:00Z"
            payload["duration_seconds"] = 539.0
        if state == "success":
            payload["result"] = {"target_vmid": 151, "guest_type": "vm", "started": False}
            if self.warnings:
                payload["result"]["warnings"] = list(self.warnings)
        if state == "failed":
            payload["error"] = self.error
        if state == "cancelled":
            payload["error"] = "Cancelled by request. The target guest may be in a partial state."
        return payload


@pytest.fixture
def scripted() -> ScriptedRestoreTransport:
    return ScriptedRestoreTransport()


@pytest.fixture
def agent(settings: Settings, scripted: ScriptedRestoreTransport) -> AgentClient:
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
) -> RestoreWorker:
    fast = settings.model_copy(
        update={"restore_idle_poll_seconds": 0.01, "agent_unreachable_grace_seconds": 1}
    )
    instance = RestoreWorker(
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


async def seed_defaults(database: Database, **gdrive: Any) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        if gdrive:
            await service.update_section(SettingsSection.GDRIVE, gdrive)
        await service.update_section(SettingsSection.AGENT, {"poll_interval_seconds": 1})


async def seed(
    database: Database,
    *,
    status: RestoreStatus = RestoreStatus.CONFIRMED,
    source: RestoreSource = RestoreSource.LOCAL,
    guest_type: GuestType = GuestType.VM,
    filename: str = ARCHIVE,
    agent_task_id: str | None = None,
    local_deleted: bool = False,
    upload_status: UploadStatus = UploadStatus.NOT_REQUIRED,
    force_stop: bool = False,
    start_after: bool = False,
    overwrite: bool = False,
) -> tuple[int, int]:
    async with database.session() as session:
        backup = BackupHistory(
            run_id=None,
            guest_id=None,
            vmid=101,
            guest_type=guest_type.value,
            guest_name="web",
            node="pve",
            storage="backup-hdd",
            filename=filename,
            size_bytes=4096,
            checksum_sha256=CHECKSUM,
            status=BackupStatus.SUCCESS.value,
            upload_status=upload_status.value,
            remote_path="gdrive:proxsync/dump" if local_deleted else None,
            local_deleted_at=datetime.now(UTC) if local_deleted else None,
            started_at=datetime.now(UTC),
        )
        session.add(backup)
        await session.flush()

        restore = RestoreHistory(
            backup_id=backup.id,
            source=source.value,
            target_vmid=151,
            target_type=guest_type.value,
            restore_mode=RestoreMode.NEW_ID.value,
            target_node="pve",
            target_storage="local-lvm",
            overwrite_existing=overwrite,
            force_stop=force_stop,
            start_after_restore=start_after,
            status=status.value,
            agent_task_id=agent_task_id,
            started_at=datetime.now(UTC) if status is RestoreStatus.RUNNING else None,
            correlation_id="test-correlation",
        )
        session.add(restore)
        await session.flush()
        return backup.id, restore.id


async def read_restore(database: Database, restore_id: int) -> RestoreHistory:
    async with database.session() as session:
        record = await SqlAlchemyRestoreRepository(session).get(restore_id)
    assert record is not None
    return record


async def read_backup(database: Database, backup_id: int) -> BackupHistory:
    async with database.session() as session:
        record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
    assert record is not None
    return record


async def run_once(worker: RestoreWorker) -> int | None:
    """Drive exactly one cycle, deterministically, without racing the loop."""
    claim = await worker._claim_next()  # noqa: SLF001 - a test seam, as in the sync worker suite
    if claim is None:
        return None
    restore_id, adopted = claim
    await worker._execute(restore_id, adopted_task=adopted)  # noqa: SLF001
    return restore_id


class TestSuccessfulRestore:
    async def test_records_the_outcome_and_the_agent_task(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        _, restore_id = await seed(database)
        scripted.states = ["running", "success"]

        await run_once(worker)

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.SUCCESS.value
        assert record.agent_task_id == "restore-1"
        assert record.finished_at is not None
        assert record.duration_seconds == 539.0

    async def test_the_agent_receives_the_frozen_options_and_the_digest(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        await seed(database, force_stop=True, start_after=True, overwrite=True)

        await run_once(worker)

        [payload] = scripted.calls("/restore/vm")
        assert payload["archive"] == ARCHIVE
        assert payload["target_vmid"] == 151
        assert payload["storage"] == "local-lvm"
        assert payload["overwrite"] is True
        assert payload["force_stop"] is True
        assert payload["start_after"] is True
        # The recorded digest is enforced on the host, before anything is spawned.
        assert payload["expected_sha256"] == CHECKSUM

    async def test_a_container_backup_uses_the_lxc_endpoint(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        await seed(database, guest_type=GuestType.LXC)

        await run_once(worker)

        assert scripted.calls("/restore/lxc")
        assert not scripted.calls("/restore/vm")

    async def test_a_guest_that_failed_to_start_is_still_a_success(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        """The restore happened. The warning has to reach the operator without inventing a
        failure the artifact does not have."""
        await seed_defaults(database)
        _, restore_id = await seed(database, start_after=True)
        scripted.warnings = ["Restore completed but guest 151 failed to start (exit 1)"]

        await run_once(worker)

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.SUCCESS.value
        assert record.error_message is not None
        assert "failed to start" in record.error_message

    async def test_the_state_change_reaches_the_event_bus(
        self, worker: RestoreWorker, database: Database, events: EventBus
    ) -> None:
        await seed_defaults(database)
        _, restore_id = await seed(database)

        async with events.subscribe() as queue:
            await run_once(worker)
            published = [queue.get_nowait() for _ in range(queue.qsize())]

        states = [event for event in published if event.type == EVENT_RESTORE_STATE]
        assert [event.data["status"] for event in states] == ["running", "success"]
        assert states[-1].data["restore_id"] == restore_id


class TestFailures:
    async def test_a_refusal_from_the_agent_fails_the_restore(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        """The agent answered, so nothing on the host changed — `failed` is the truth."""
        await seed_defaults(database)
        _, restore_id = await seed(database)
        scripted.accept = False

        await run_once(worker)

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.FAILED.value
        assert record.error_message is not None
        assert "already exists" in record.error_message

    async def test_a_failed_command_is_recorded_with_the_agents_reason(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        _, restore_id = await seed(database)
        scripted.states = ["running", "failed"]

        await run_once(worker)

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.FAILED.value
        assert record.error_message == scripted.error

    async def test_an_unreachable_agent_at_start_is_interrupted_not_failed(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        """A lost response is indistinguishable from a lost request. The restore may be
        running right now, and the row must not claim otherwise."""
        await seed_defaults(database)
        _, restore_id = await seed(database)
        scripted.unreachable = True

        await run_once(worker)

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.INTERRUPTED.value
        assert record.error_message is not None
        assert "may still have arrived" in record.error_message

    async def test_a_task_the_agent_has_forgotten_is_interrupted(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        _, restore_id = await seed(database)
        scripted.states = ["running"]
        scripted.unknown_tasks = {"restore-1"}

        await run_once(worker)

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.INTERRUPTED.value

    async def test_a_backup_without_an_artifact_fails_before_the_host_is_touched(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        backup_id, restore_id = await seed(database)
        async with database.session() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
            assert record is not None
            record.filename = None

        await run_once(worker)

        assert (await read_restore(database, restore_id)).status == RestoreStatus.FAILED.value
        assert not scripted.calls("/restore/vm")


class TestDriveSourcedRestore:
    async def test_the_archive_is_downloaded_before_the_restore_starts(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database, enabled=True, remote_name="gdrive", folder="proxsync/dump")
        backup_id, restore_id = await seed(
            database,
            source=RestoreSource.GDRIVE,
            local_deleted=True,
            upload_status=UploadStatus.UPLOADED,
        )

        await run_once(worker)

        [download] = scripted.calls("/sync/download")
        assert download["filename"] == ARCHIVE
        assert download["remote"] == "gdrive"
        # Refused by default, so a download can never clobber a good local artifact.
        assert download["overwrite"] is False

        ordering = [request.url.path for request in scripted.requests]
        assert ordering.index("/sync/download") < ordering.index("/restore/vm")

        assert (await read_restore(database, restore_id)).status == RestoreStatus.SUCCESS.value
        # The artifact is on the host again, so the row must stop claiming it is not.
        assert (await read_backup(database, backup_id)).local_deleted_at is None

    async def test_a_failed_download_never_starts_the_restore(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database, enabled=True)
        backup_id, restore_id = await seed(
            database,
            source=RestoreSource.GDRIVE,
            local_deleted=True,
            upload_status=UploadStatus.UPLOADED,
        )
        scripted.download_states = ["failed"]

        await run_once(worker)

        assert not scripted.calls("/restore/vm")
        assert (await read_restore(database, restore_id)).status == RestoreStatus.FAILED.value
        assert (await read_backup(database, backup_id)).local_deleted_at is not None

    async def test_drive_disabled_is_refused_rather_than_attempted(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database, enabled=False)
        _, restore_id = await seed(
            database,
            source=RestoreSource.GDRIVE,
            local_deleted=True,
            upload_status=UploadStatus.UPLOADED,
        )

        await run_once(worker)

        assert not scripted.calls("/sync/download")
        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.FAILED.value
        assert record.error_message is not None
        assert "Drive sync is disabled" in record.error_message


class TestQueueAndRecovery:
    async def test_the_queue_is_not_a_stack(
        self, worker: RestoreWorker, database: Database
    ) -> None:
        """Oldest first. Claiming newest-first would let a restore submitted during a backlog
        starve — the same bug the backup queue had before M3."""
        await seed_defaults(database)
        _, first = await seed(database)
        _, second = await seed(database, filename="vzdump-qemu-102-2026_07_26-02_00_00.vma.zst")

        claim = await worker._claim_next()  # noqa: SLF001

        assert second > first
        assert claim == (first, None)

    async def test_a_restore_with_no_agent_task_is_interrupted_on_restart(
        self, worker: RestoreWorker, database: Database
    ) -> None:
        """We died before the agent confirmed it. It is never re-issued: a second qmrestore
        over a running one is the worst outcome available."""
        await seed_defaults(database)
        _, restore_id = await seed(database, status=RestoreStatus.RUNNING)

        await worker.recover()

        record = await read_restore(database, restore_id)
        assert record.status == RestoreStatus.INTERRUPTED.value
        assert record.error_message is not None
        assert "may still have reached the host" in record.error_message

    async def test_a_live_agent_task_is_adopted_rather_than_restarted(
        self, worker: RestoreWorker, database: Database, scripted: ScriptedRestoreTransport
    ) -> None:
        await seed_defaults(database)
        # Give the transport a task with this id by accepting one restore up front.
        scripted.states = ["running", "success"]
        accepted = await worker._agent.start_restore(  # noqa: SLF001
            guest_type=GuestType.VM, archive=ARCHIVE, target_vmid=151, storage="local-lvm"
        )
        _, restore_id = await seed(
            database, status=RestoreStatus.RUNNING, agent_task_id=accepted.task_id
        )

        await worker.recover()
        await run_once(worker)

        assert len(scripted.calls("/restore/vm")) == 1  # nothing new was started
        assert (await read_restore(database, restore_id)).status == RestoreStatus.SUCCESS.value

    async def test_expired_confirmations_are_swept(
        self, worker: RestoreWorker, database: Database
    ) -> None:
        await seed_defaults(database)
        _, restore_id = await seed(database, status=RestoreStatus.PENDING_CONFIRMATION)
        async with database.session() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert record is not None
            record.confirmation_expires_at = datetime(2020, 1, 1, tzinfo=UTC)

        await worker._expire_stale()  # noqa: SLF001

        assert (await read_restore(database, restore_id)).status == RestoreStatus.EXPIRED.value

    async def test_cancelling_a_restore_this_worker_is_not_running_is_refused(
        self, worker: RestoreWorker, database: Database
    ) -> None:
        await seed_defaults(database)
        _, restore_id = await seed(database, status=RestoreStatus.RUNNING)

        assert await worker.cancel(restore_id) is False

    async def test_the_claim_holds_the_retention_guard(
        self, settings: Settings, database: Database, agent: AgentClient, events: EventBus
    ) -> None:
        """A cancel arriving while this worker claims must not land on a running restore.

        Guarded routes read the row inside `retention_guard.transaction()`; the claim takes the
        same guard, so the two cannot interleave and see each other's half-applied state.
        """
        guard = RetentionGuard()
        instance = RestoreWorker(
            database=database,
            agent=agent,
            events=events,
            secret_box=SecretBox(SECRET_KEY),
            settings=settings,
            retention_guard=guard,
        )
        await seed_defaults(database)
        _, restore_id = await seed(database)

        async with guard.hold():
            claim = asyncio.wait_for(instance._claim_next(), timeout=0.2)  # noqa: SLF001
            with pytest.raises(TimeoutError):
                await claim
            # Still confirmed: the claim never got far enough to flip it.
            assert (await read_restore(database, restore_id)).status == (
                RestoreStatus.CONFIRMED.value
            )

        assert await instance._claim_next() == (restore_id, None)  # noqa: SLF001
