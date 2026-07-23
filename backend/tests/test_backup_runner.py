"""The run executor.

The interesting cases are not the happy path. They are: what a restart does to a backup that
was in flight, what happens when the agent stops answering, and the invariant that a lost
response never causes a second `vzdump`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EventBus
from app.db.models.backup import BackupHistory, BackupRun
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.schemas.backup import ResolvedTarget, RunOptions
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestType,
    RunStatus,
    TriggerType,
    UploadStatus,
)
from app.workers.backup_runner import BackupRunner

from .conftest import SECRET_KEY, RecordedRequest, make_guest


def archive_for(vmid: int, guest_type: GuestType) -> str:
    kind = "qemu" if guest_type is GuestType.VM else "lxc"
    suffix = "vma.zst" if guest_type is GuestType.VM else "tar.zst"
    return f"vzdump-{kind}-{vmid}-2026_07_26-01_00_04.{suffix}"


class ScriptedAgentTransport(httpx.AsyncBaseTransport):
    """A programmable agent.

    Each `POST /backup/start` mints its own task, exactly as the real agent does, so a run
    over several guests produces several tasks with distinct artifact names. A test that
    reused one task id would never have caught the artifact-name collision this models.

    `states` is the sequence a task walks through as it is polled — `["running", "success"]`
    reproduces the polling the runner actually performs.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.states: list[str] = ["success"]
        self.states_by_vmid: dict[int, list[str]] = {}
        self.error = "vzdump failed"
        self.unreachable = False
        self.cancelled: list[str] = []

        self._tasks: dict[str, dict[str, Any]] = {}
        self._remaining: dict[str, list[str]] = {}
        self._polls: dict[str, int] = {}
        self._counter = 0

    def set_states(self, vmid: int, states: list[str]) -> None:
        self.states_by_vmid[vmid] = states

    def preload(self, task_id: str, *, vmid: int, state: str) -> None:
        """Register a task the agent already knows about — what recovery finds after a crash."""
        self._tasks[task_id] = {"vmid": vmid, "guest_type": GuestType.VM}
        self._remaining[task_id] = [state]

    @property
    def started_backups(self) -> list[dict[str, Any]]:
        return [
            request.json_body
            for request in self.requests
            if request.method == "POST" and request.url.path.endswith("/backup/start")
        ]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(RecordedRequest(request))
        if self.unreachable:
            raise httpx.ConnectError("agent is down", request=request)

        path = request.url.path
        if path.endswith("/storage/status"):
            return httpx.Response(
                200,
                json={
                    "dump_root": {
                        "path": "/mnt/backup-hdd/dump",
                        "total_bytes": 1_000,
                        "used_bytes": 500,
                        "free_bytes": 500,
                        "used_percent": 50.0,
                    },
                    "storages": [],
                    "artifact_count": 0,
                    "artifact_bytes": 0,
                },
            )
        if path.endswith("/backup/start"):
            return self._start(request)
        if path.endswith("/cancel"):
            return self._cancel(path.rsplit("/", 2)[-2])
        if "/task/" in path:
            return self._poll(path.rsplit("/", 1)[-1])
        return httpx.Response(404, json={"detail": f"unhandled {path}"})

    def _start(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        payload = _json.loads(request.content)
        self._counter += 1
        task_id = f"task-{self._counter}"
        vmid = int(payload["vmid"])

        self._tasks[task_id] = {"vmid": vmid, "guest_type": GuestType(payload["guest_type"])}
        self._remaining[task_id] = list(self.states_by_vmid.get(vmid, self.states))

        return httpx.Response(
            202,
            json={
                "task_id": task_id,
                "kind": "backup",
                "state": "queued",
                "created_at": "2026-07-26T01:00:00Z",
            },
        )

    def _cancel(self, task_id: str) -> httpx.Response:
        self.cancelled.append(task_id)
        self._remaining[task_id] = ["cancelled"]
        return httpx.Response(200, json=self._render(task_id, "cancelled"))

    def _poll(self, task_id: str) -> httpx.Response:
        remaining = self._remaining.get(task_id)
        if remaining is None:
            return httpx.Response(404, json={"detail": "unknown task"})
        # The last state is terminal and sticky, exactly like the agent's task journal.
        state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(200, json=self._render(task_id, state))

    def _render(self, task_id: str, state: str) -> dict[str, Any]:
        meta = self._tasks.get(task_id, {"vmid": 0, "guest_type": GuestType.VM})
        payload: dict[str, Any] = {
            "task_id": task_id,
            "kind": "backup",
            "state": state,
            "created_at": "2026-07-26T01:00:00Z",
            "started_at": "2026-07-26T01:00:01Z",
            "progress": {},
            "result": {},
            "meta": meta | {"guest_type": meta["guest_type"].value},
            "log_path": f"/var/log/proxsync-agent/tasks/{task_id}.log",
        }
        if state == "running":
            # Per-task, so a test can assert the exact sequence of published percentages.
            self._polls[task_id] = self._polls.get(task_id, 0) + 1
            payload["progress"] = {
                "percent": 25.0 * self._polls[task_id],
                "bytes_done": 1024,
                "bytes_total": 2048,
            }
        if state in {"success", "failed", "cancelled"}:
            payload["finished_at"] = "2026-07-26T01:12:00Z"
            payload["duration_seconds"] = 719.0
        if state == "success":
            archive = archive_for(meta["vmid"], meta["guest_type"])
            payload["exit_code"] = 0
            payload["result"] = {
                "filename": archive,
                "path": f"/mnt/backup-hdd/dump/{archive}",
                "size_bytes": 5_368_709_120,
                "checksum_sha256": "a" * 64,
                "duration_seconds": 719.0,
            }
        if state == "failed":
            payload["exit_code"] = 2
            payload["error"] = self.error
        return payload


@pytest.fixture
def agent(settings: Settings, scripted: ScriptedAgentTransport) -> AgentClient:
    return AgentClient(
        settings,
        client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=scripted),
    )


@pytest.fixture
def scripted() -> ScriptedAgentTransport:
    return ScriptedAgentTransport()


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
def runner(
    settings: Settings, database: Database, agent: AgentClient, events: EventBus
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
    )
    # The poll interval has a one-second floor in the settings schema, which is right for
    # production and far too slow for a suite that asserts sequences rather than timings.
    # `TestPollInterval` covers the real lookup.
    instance._poll_interval = _instant  # type: ignore[method-assign]  # noqa: SLF001
    return instance


async def _instant() -> float:
    return 0.001


async def make_run(
    database: Database,
    *,
    targets: list[tuple[int, GuestType]],
    upload: bool = False,
    status: RunStatus = RunStatus.QUEUED,
) -> int:
    # Real guest rows: `backup_history.guest_id` is a foreign key, and the SQLite pragmas
    # enforce it here exactly as they do in production.
    resolved: list[ResolvedTarget] = []
    async with database.session() as session:
        for vmid, guest_type in targets:
            guest = make_guest(vmid, guest_type=guest_type, name=f"guest-{vmid}")
            session.add(guest)
            await session.flush()
            resolved.append(
                ResolvedTarget(
                    guest_id=guest.id,
                    vmid=vmid,
                    guest_type=guest_type,
                    guest_name=guest.name,
                    node=guest.node,
                )
            )

    async with database.session() as session:
        run = await SqlAlchemyBackupRunRepository(session).create(
            job_id=None,
            trigger=TriggerType.MANUAL,
            requested_by=None,
            targets=[target.model_dump(mode="json") for target in resolved],
            correlation_id="test-correlation",
        )
        run.status = status.value
        run.options = RunOptions(
            mode=BackupMode.SNAPSHOT,
            compression=Compression.ZSTD,
            storage="backup-hdd",
            upload=upload,
        ).model_dump(mode="json")
        return run.id


async def history_for(database: Database, run_id: int) -> list[BackupHistory]:
    async with database.session() as session:
        return await SqlAlchemyBackupHistoryRepository(session).for_run(run_id)


async def read_run(database: Database, run_id: int) -> BackupRun:
    async with database.session() as session:
        run = await SqlAlchemyBackupRunRepository(session).get(run_id)
    assert run is not None
    return run


class TestHappyPath:
    async def test_a_run_backs_up_every_guest_and_records_the_artifact(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.states = ["running", "success"]
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        await runner._execute_run(run_id)  # noqa: SLF001 - driving the worker directly

        run = await read_run(database, run_id)
        assert run.status == RunStatus.SUCCESS.value
        assert (run.guest_succeeded, run.guest_failed) == (1, 0)
        assert run.duration_seconds is not None

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.SUCCESS.value
        assert record.filename == archive_for(101, GuestType.VM)
        assert record.size_bytes == 5_368_709_120
        assert record.checksum_sha256 == "a" * 64
        assert record.agent_task_id == "task-1"
        assert record.log_path is not None

    async def test_the_agent_receives_the_runs_frozen_options(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.states = ["success"]
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        await runner._execute_run(run_id)  # noqa: SLF001

        [started] = scripted.started_backups
        assert started["vmid"] == 101
        assert started["guest_type"] == "vm"
        assert started["mode"] == "snapshot"
        assert started["compression"] == "zstd"
        assert started["storage"] == "backup-hdd"

    async def test_guests_are_backed_up_one_at_a_time(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        """The agent holds a single backup slot; overlapping would just earn a 409."""
        scripted.states = ["success"]
        run_id = await make_run(database, targets=[(101, GuestType.VM), (201, GuestType.LXC)])

        await runner._execute_run(run_id)  # noqa: SLF001

        paths = [r.url.path for r in scripted.requests]
        first_start = paths.index("/backup/start")
        second_start = paths.index("/backup/start", first_start + 1)
        assert "/task/task-1" in paths[first_start:second_start], (
            "the first backup must reach a terminal state before the second starts"
        )

    async def test_upload_is_pending_only_when_the_run_asked_for_it(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.states = ["success"]
        run_id = await make_run(database, targets=[(101, GuestType.VM)], upload=True)

        await runner._execute_run(run_id)  # noqa: SLF001

        [record] = await history_for(database, run_id)
        assert record.upload_status == UploadStatus.PENDING.value

    async def test_progress_is_published_but_not_persisted(
        self,
        runner: BackupRunner,
        database: Database,
        scripted: ScriptedAgentTransport,
        events: EventBus,
    ) -> None:
        scripted.states = ["running", "running", "success"]
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        async with events.subscribe() as queue:
            await runner._execute_run(run_id)  # noqa: SLF001
            frames = [queue.get_nowait() for _ in range(queue.qsize())]

        percentages = [frame.data["percent"] for frame in frames if frame.type == "backup.progress"]
        assert percentages == [25.0, 50.0], "one frame per poll, in order"
        assert runner.progress_for(run_id) is None, "progress is cleared when the run ends"


class TestFailures:
    async def test_a_failed_backup_is_recorded_with_the_agents_reason(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.states = ["failed"]
        scripted.error = "vzdump exited 2: no space left on device"
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        await runner._execute_run(run_id)  # noqa: SLF001

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.FAILED.value
        assert "no space left" in (record.error_message or "")
        assert record.upload_status == UploadStatus.NOT_REQUIRED.value

        run = await read_run(database, run_id)
        assert run.status == RunStatus.FAILED.value
        assert run.guest_failed == 1

    async def test_one_failure_does_not_abandon_the_remaining_guests(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.set_states(101, ["failed"])  # 201 keeps the default success
        run_id = await make_run(database, targets=[(101, GuestType.VM), (201, GuestType.LXC)])

        await runner._execute_run(run_id)  # noqa: SLF001

        records = await history_for(database, run_id)
        assert len(records) == 2

        run = await read_run(database, run_id)
        assert run.status == RunStatus.PARTIAL.value, "nine of ten is neither success nor failure"

    async def test_an_unreachable_agent_fails_the_guest_without_a_task_id(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.unreachable = True
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        await runner._execute_run(run_id)  # noqa: SLF001

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.FAILED.value
        assert record.agent_task_id is None
        assert "Backup Agent" in (record.error_message or "")

    async def test_an_agent_that_stops_answering_mid_backup_yields_interrupted(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        """`failed` would be a lie: the vzdump may well have completed on the host."""
        scripted.states = ["running"]  # never reaches a terminal state
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        original = runner._poll_interval  # noqa: SLF001

        async def poll_then_die() -> float:
            scripted.unreachable = True
            return await original()

        runner._poll_interval = poll_then_die  # type: ignore[method-assign]  # noqa: SLF001
        await runner._execute_run(run_id)  # noqa: SLF001

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.INTERRUPTED.value
        assert "outcome is unknown" in (record.error_message or "")


class TestCancellation:
    async def test_cancelling_stops_the_current_backup_and_skips_the_rest(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.states = ["running"]
        run_id = await make_run(database, targets=[(101, GuestType.VM), (201, GuestType.LXC)])

        original = runner._poll_interval  # noqa: SLF001

        async def cancel_once_running() -> float:
            interval = await original()
            await runner.cancel(run_id)
            return interval

        runner._poll_interval = cancel_once_running  # type: ignore[method-assign]  # noqa: SLF001
        await runner._execute_run(run_id)  # noqa: SLF001

        assert scripted.cancelled == ["task-1"]
        records = await history_for(database, run_id)
        assert len(records) == 1, "the second guest was never started"
        assert records[0].status == BackupStatus.CANCELLED.value

        run = await read_run(database, run_id)
        assert run.status == RunStatus.CANCELLED.value

    async def test_a_cancelled_backup_is_not_counted_as_a_failure(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        """The dashboard's failed-backup tile must not include deliberate cancellations."""
        scripted.states = ["cancelled"]
        run_id = await make_run(database, targets=[(101, GuestType.VM)])

        await runner._execute_run(run_id)  # noqa: SLF001

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.CANCELLED.value
        assert record.status != BackupStatus.FAILED.value


class TestRecovery:
    async def test_a_backup_with_no_task_id_is_interrupted_never_retried(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        """The request may have reached the host. A retry could start a second vzdump."""
        run_id = await make_run(database, targets=[(101, GuestType.VM)], status=RunStatus.RUNNING)
        async with database.session() as session:
            await SqlAlchemyBackupHistoryRepository(session).create(
                run_id=run_id,
                guest_id=None,
                vmid=101,
                guest_type=GuestType.VM,
                guest_name="web",
                node="pve",
                storage="backup-hdd",
                mode=BackupMode.SNAPSHOT,
                compression=Compression.ZSTD,
                upload_status=UploadStatus.PENDING,
                correlation_id=None,
                started_at=datetime.now(UTC),
            )

        await runner.recover()

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.INTERRUPTED.value
        assert "not retried automatically" in (record.error_message or "")
        assert scripted.started_backups == [], "recovery must never start a backup"

    async def test_a_task_the_agent_finished_is_adopted_with_its_real_outcome(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.preload("task-9", vmid=101, state="success")
        run_id = await make_run(database, targets=[(101, GuestType.VM)], status=RunStatus.RUNNING)
        await _seed_running_backup(database, run_id, task_id="task-9")

        await runner.recover()

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.SUCCESS.value
        assert record.filename == archive_for(101, GuestType.VM)
        assert scripted.started_backups == []

    async def test_a_task_the_agent_has_forgotten_becomes_interrupted(
        self, runner: BackupRunner, database: Database
    ) -> None:
        run_id = await make_run(database, targets=[(101, GuestType.VM)], status=RunStatus.RUNNING)
        await _seed_running_backup(database, run_id, task_id="task-gone")

        await runner.recover()

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.INTERRUPTED.value
        assert "no longer has a record" in (record.error_message or "")

    async def test_an_interrupted_run_is_requeued(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.preload("task-9", vmid=101, state="success")
        run_id = await make_run(database, targets=[(101, GuestType.VM)], status=RunStatus.RUNNING)
        await _seed_running_backup(database, run_id, task_id="task-9")

        await runner.recover()

        run = await read_run(database, run_id)
        assert run.status == RunStatus.QUEUED.value

    async def test_a_resumed_run_skips_guests_it_already_finished(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        scripted.states = ["success"]
        run_id = await make_run(
            database,
            targets=[(101, GuestType.VM), (201, GuestType.LXC)],
            status=RunStatus.RUNNING,
        )
        async with database.session() as session:
            done = await SqlAlchemyBackupHistoryRepository(session).create(
                run_id=run_id,
                guest_id=None,
                vmid=101,
                guest_type=GuestType.VM,
                guest_name="guest-101",
                node="pve",
                storage="backup-hdd",
                mode=BackupMode.SNAPSHOT,
                compression=Compression.ZSTD,
                upload_status=UploadStatus.NOT_REQUIRED,
                correlation_id=None,
                started_at=datetime.now(UTC),
            )
            done.status = BackupStatus.SUCCESS.value
            done.filename = "already-done.vma.zst"

        await runner._execute_run(run_id)  # noqa: SLF001

        assert [start["vmid"] for start in scripted.started_backups] == [201], (
            "the guest already backed up must not be backed up again"
        )
        records = await history_for(database, run_id)
        assert len(records) == 2

    async def test_deferred_when_the_agent_is_not_up_yet(
        self, runner: BackupRunner, database: Database, scripted: ScriptedAgentTransport
    ) -> None:
        """The agent may simply be starting more slowly than the dashboard."""
        run_id = await make_run(database, targets=[(101, GuestType.VM)], status=RunStatus.RUNNING)
        await _seed_running_backup(database, run_id, task_id="task-9")
        scripted.unreachable = True

        await runner.recover()

        [record] = await history_for(database, run_id)
        assert record.status == BackupStatus.RUNNING.value, "left alone for the next attempt"


class TestQueue:
    async def test_claims_the_oldest_queued_run_first(
        self, runner: BackupRunner, database: Database
    ) -> None:
        first = await make_run(database, targets=[(101, GuestType.VM)])
        await make_run(database, targets=[(102, GuestType.VM)])

        claimed = await runner._claim_next_run()  # noqa: SLF001

        assert claimed == first
        run = await read_run(database, first)
        assert run.status == RunStatus.RUNNING.value
        assert run.started_at is not None

    async def test_an_empty_queue_claims_nothing(
        self, runner: BackupRunner, database: Database
    ) -> None:
        assert await runner._claim_next_run() is None  # noqa: SLF001

    async def test_resuming_does_not_overwrite_the_original_start_time(
        self, runner: BackupRunner, database: Database
    ) -> None:
        run_id = await make_run(database, targets=[(101, GuestType.VM)])
        started = datetime(2026, 7, 26, 1, 0, tzinfo=UTC)
        async with database.session() as session:
            run = await SqlAlchemyBackupRunRepository(session).get(run_id)
            assert run is not None
            run.started_at = started

        await runner._claim_next_run()  # noqa: SLF001

        run = await read_run(database, run_id)
        assert run.started_at == started

    async def test_the_doorbell_is_rung_on_commit_not_before(
        self, runner: BackupRunner, database: Database
    ) -> None:
        session = database.session_factory()
        try:
            runner.notify_after_commit(session)
            assert not runner._doorbell.is_set()  # noqa: SLF001 - asserting the timing
            await session.commit()
            assert runner._doorbell.is_set()  # noqa: SLF001
        finally:
            await session.close()


async def _seed_running_backup(database: Database, run_id: int, *, task_id: str) -> int:
    async with database.session() as session:
        run = await SqlAlchemyBackupRunRepository(session).get(run_id)
        assert run is not None
        guest_id = (run.targets or [{}])[0].get("guest_id")
        record = await SqlAlchemyBackupHistoryRepository(session).create(
            run_id=run_id,
            guest_id=guest_id,
            vmid=101,
            guest_type=GuestType.VM,
            guest_name="guest-101",
            node="pve",
            storage="backup-hdd",
            mode=BackupMode.SNAPSHOT,
            compression=Compression.ZSTD,
            upload_status=UploadStatus.PENDING,
            correlation_id=None,
            started_at=datetime.now(UTC),
        )
        record.agent_task_id = task_id
        return record.id
