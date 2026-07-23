"""The run executor: the only component that drives a backup from request to history row.

**The database is the queue.** A run is durable in `backup_runs` before anything is started;
the in-memory doorbell only removes latency. That single decision buys three properties at
once: a crash loses nothing, restart recovery is the same code path as normal operation, and
the scheduler and the API can both submit work without knowing about each other.

**Backups are serialised.** The agent holds one backup slot, so running two at a time would
merely earn a 409 from it. One worker, one run, one guest at a time.

**A lost response never re-issues a backup.** `vzdump` is not idempotent; starting it twice
would produce two archives and skew retention. Where the outcome is unknown — the agent was
restarted, or stopped answering — the row is recorded as `interrupted` and left for a human,
never retried automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import AgentError, AgentUnavailable, AppError
from app.core.events import (
    EVENT_BACKUP_PROGRESS,
    EVENT_BACKUP_STATE,
    EVENT_RUN_STATE,
    EventBus,
)
from app.core.logging import logger, set_correlation_id
from app.core.retention_guard import RetentionGuard
from app.db.models.backup import BackupHistory
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.agent import AgentTask
from app.schemas.backup import ResolvedTarget, RunOptions, RunProgress
from app.schemas.enums import (
    BackupStatus,
    NotificationEvent,
    RunStatus,
    SettingsSection,
    UploadStatus,
)
from app.schemas.settings import AgentSettings
from app.services.backup_service import run_payload, summarise_run
from app.services.notification_service import NotificationGateway
from app.services.settings_service import SettingsService

_AGENT_STATE_TO_BACKUP_STATUS = {
    "success": BackupStatus.SUCCESS,
    "failed": BackupStatus.FAILED,
    "cancelled": BackupStatus.CANCELLED,
    "interrupted": BackupStatus.INTERRUPTED,
}

RECOVERY_SCAN_LIMIT = 1000


class RetentionNotifier(Protocol):
    """Small seam that keeps the backup executor independent of retention's implementation."""

    def notify(self, backup_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class RunPlan:
    """A run, read out of the database into the shape the executor works in."""

    options: RunOptions
    targets: list[ResolvedTarget]
    completed: set[tuple[int, str]]
    """Guests this run already finished — populated after a restart, empty otherwise."""
    adoptable: dict[tuple[int, str], int]
    """Guests whose backup was still running when the previous process died, by history id."""
    correlation_id: str


class BackupRunner:
    def __init__(
        self,
        *,
        database: Database,
        agent: AgentClient,
        events: EventBus,
        secret_box: SecretBox,
        settings: Settings,
        retention: RetentionNotifier | None = None,
        retention_guard: RetentionGuard | None = None,
        notifications: NotificationGateway | None = None,
    ) -> None:
        self._database = database
        self._agent = agent
        self._events = events
        self._secret_box = secret_box
        self._settings = settings
        self._retention = retention
        self._retention_guard = retention_guard or RetentionGuard()
        self._notifications = notifications

        self._task: asyncio.Task[None] | None = None
        self._doorbell = asyncio.Event()
        self._stopping = asyncio.Event()
        self._progress: dict[int, RunProgress] = {}
        self._cancelled: set[int] = set()
        self._current_run_id: int | None = None
        self._current_task_id: str | None = None

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.recover()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="proxsync-backup-runner")
        logger.info("backup_runner_started")

    async def stop(self) -> None:
        self._stopping.set()
        self._doorbell.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10)
            self._task = None
        logger.info("backup_runner_stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_run_id(self) -> int | None:
        return self._current_run_id

    def notify(self) -> None:
        """Ring the doorbell. Safe to call spuriously; the worker just re-queries."""
        self._doorbell.set()

    def notify_after_commit(self, session: AsyncSession) -> None:
        """Ring the doorbell when *this* transaction commits.

        Calling `notify()` directly from a request handler would race the session dependency,
        which commits after the handler returns: the worker could query before the run row
        was visible and go back to sleep.
        """

        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _ring(_session: Session) -> None:
            self._doorbell.set()

    def progress_for(self, run_id: int) -> RunProgress | None:
        return self._progress.get(run_id)

    # ---- queue ---------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            # Cleared before claiming, so a notify that arrives *during* a claim is not lost.
            self._doorbell.clear()
            try:
                run_id = await self._claim_next_run()
            except Exception:  # noqa: BLE001 - the worker must outlive a bad iteration
                logger.error("backup_runner_claim_failed", exc_info=True)
                await self._sleep(self._settings.run_idle_poll_seconds)
                continue

            if run_id is None:
                await self._wait_for_work()
                continue

            try:
                await self._execute_run(run_id)
            except Exception:  # noqa: BLE001 - one broken run must not stop the queue
                logger.error("backup_run_crashed", run_id=run_id, exc_info=True)
                await self._finalise_run(run_id, error="The run executor failed unexpectedly")
            finally:
                self._current_run_id = None
                self._current_task_id = None

    async def _wait_for_work(self) -> None:
        """Sleep until the doorbell rings, or until the idle poll comes round.

        The poll is the backstop: a doorbell rung a moment before its transaction committed
        would otherwise leave the run sitting until the next one.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._doorbell.wait(), timeout=self._settings.run_idle_poll_seconds
            )

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _claim_next_run(self) -> int | None:
        async with self._database.session() as session:
            runs = SqlAlchemyBackupRunRepository(session)
            run = await runs.next_queued()
            if run is None:
                return None
            run.status = RunStatus.RUNNING.value
            # Preserved across a resume: overwriting it would report a run that has been
            # going for an hour as having just started.
            if run.started_at is None:
                run.started_at = datetime.now(UTC)
            await runs.flush()
            return run.id

    # ---- execution -----------------------------------------------------------

    async def _execute_run(self, run_id: int) -> None:
        plan = await self._load_plan(run_id)
        if plan is None:
            return

        set_correlation_id(plan.correlation_id or None)
        self._current_run_id = run_id
        self._cancelled.discard(run_id)
        self._events.publish(EVENT_RUN_STATE, run_id=run_id, status=RunStatus.RUNNING.value)

        logger.info(
            "backup_run_started",
            run_id=run_id,
            guests=len(plan.targets),
            already_done=len(plan.completed),
            storage=plan.options.storage,
        )

        for target in plan.targets:
            if self._stopping.is_set():
                # Leave the run `running`: recovery re-queues it and picks up where we left
                # off. Marking it failed here would lose the guests already backed up.
                logger.info("backup_run_paused_for_shutdown", run_id=run_id, vmid=target.vmid)
                return
            if run_id in self._cancelled:
                logger.info("backup_run_cancelled", run_id=run_id, stopped_before_vmid=target.vmid)
                break

            key = (target.vmid, target.guest_type.value)
            if key in plan.completed:
                continue

            await self._backup_guest(
                run_id=run_id,
                target=target,
                options=plan.options,
                adopt_id=plan.adoptable.get(key),
            )

        await self._finalise_run(run_id)

    async def _load_plan(self, run_id: int) -> RunPlan | None:
        async with self._database.session() as session:
            run = await SqlAlchemyBackupRunRepository(session).get(run_id)
            if run is None:  # pragma: no cover - the row was just claimed
                logger.error("backup_run_vanished", run_id=run_id)
                return None

            # Normally set when the run was claimed. Set here too so that the duration is
            # never silently null for a run that reached execution by another path.
            if run.started_at is None:
                run.started_at = datetime.now(UTC)

            records = await SqlAlchemyBackupHistoryRepository(session).for_run(run_id)
            plan = RunPlan(
                options=RunOptions.model_validate(run.options or {}),
                targets=[ResolvedTarget.model_validate(item) for item in (run.targets or [])],
                completed={
                    (record.vmid, record.guest_type)
                    for record in records
                    if BackupStatus(record.status).is_terminal
                },
                adoptable={
                    (record.vmid, record.guest_type): record.id
                    for record in records
                    if record.status == BackupStatus.RUNNING.value
                },
                correlation_id=run.correlation_id,
            )
            if self._notifications is not None:
                # Keyed uniquely per run, so a resume after a restart does not announce a
                # second start for work that is merely continuing.
                await self._notifications.emit(
                    session,
                    NotificationEvent.BACKUP_STARTED,
                    dedupe_key=f"run:{run_id}:started",
                    unique=True,
                    variables={
                        "run_id": run_id,
                        "guest_total": len(plan.targets),
                        "trigger": run.trigger,
                        "storage": plan.options.storage,
                        "guests": [
                            {
                                "guest_name": target.guest_name,
                                "vmid": target.vmid,
                                "guest_type": target.guest_type.value,
                            }
                            for target in plan.targets
                        ],
                    },
                )
            return plan

    async def _backup_guest(
        self,
        *,
        run_id: int,
        target: ResolvedTarget,
        options: RunOptions,
        adopt_id: int | None,
    ) -> None:
        backup_id, task_id = await self._open_backup(
            run_id=run_id, target=target, options=options, adopt_id=adopt_id
        )

        if task_id is None:
            task_id = await self._start_agent_backup(
                backup_id=backup_id, target=target, options=options
            )
            if task_id is None:
                return

        self._current_task_id = task_id
        task = await self._poll_to_terminal(
            task_id, run_id=run_id, backup_id=backup_id, target=target
        )
        self._current_task_id = None
        self._progress.pop(run_id, None)

        if task is None:
            # Shutdown, or the agent stopped answering. The row stays as `_poll_to_terminal`
            # left it; recovery decides on the next start.
            return

        await self._record_outcome(
            backup_id=backup_id, run_id=run_id, task=task, upload=options.upload
        )

    async def _open_backup(
        self,
        *,
        run_id: int,
        target: ResolvedTarget,
        options: RunOptions,
        adopt_id: int | None,
    ) -> tuple[int, str | None]:
        """Create the history row, or pick up the one a previous process left behind."""
        async with self._database.session() as session:
            history = SqlAlchemyBackupHistoryRepository(session)
            if adopt_id is not None:
                adopted = await history.get(adopt_id)
                if adopted is not None:
                    logger.info(
                        "backup_readopted",
                        backup_id=adopted.id,
                        run_id=run_id,
                        vmid=target.vmid,
                        agent_task_id=adopted.agent_task_id,
                    )
                    return adopted.id, adopted.agent_task_id

            record = await history.create(
                run_id=run_id,
                guest_id=target.guest_id,
                vmid=target.vmid,
                guest_type=target.guest_type,
                guest_name=target.guest_name,
                node=target.node,
                storage=options.storage,
                mode=options.mode,
                compression=options.compression,
                upload_status=(
                    UploadStatus.PENDING if options.upload else UploadStatus.NOT_REQUIRED
                ),
                correlation_id=None,
                started_at=datetime.now(UTC),
            )
            backup_id = record.id

        self._events.publish(
            EVENT_BACKUP_STATE,
            backup_id=backup_id,
            run_id=run_id,
            vmid=target.vmid,
            guest_name=target.guest_name,
            status=BackupStatus.RUNNING.value,
        )
        return backup_id, None

    async def _start_agent_backup(
        self, *, backup_id: int, target: ResolvedTarget, options: RunOptions
    ) -> str | None:
        try:
            accepted = await self._agent.start_backup(
                vmid=target.vmid,
                guest_type=target.guest_type,
                mode=options.mode,
                compression=options.compression,
                storage=options.storage,
                zstd_threads=options.zstd_threads,
                bwlimit_kbps=options.bwlimit_kbps,
                notes=options.notes,
            )
        except (AgentUnavailable, AgentError) as exc:
            await self._fail_backup(backup_id, str(exc.detail))
            return None
        except AppError as exc:  # configuration errors, validation refusals
            await self._fail_backup(backup_id, str(exc.detail))
            return None

        async with self._database.session() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
            if record is not None:
                record.agent_task_id = accepted.task_id
                record.correlation_id = accepted.correlation_id

        logger.info(
            "backup_started",
            backup_id=backup_id,
            vmid=target.vmid,
            guest_type=target.guest_type.value,
            agent_task_id=accepted.task_id,
        )
        return accepted.task_id

    async def _poll_to_terminal(
        self, task_id: str, *, run_id: int, backup_id: int, target: ResolvedTarget
    ) -> AgentTask | None:
        interval = await self._poll_interval()
        unreachable_since: float | None = None
        grace = self._settings.agent_unreachable_grace_seconds

        while not self._stopping.is_set():
            try:
                task = await self._agent.get_task(task_id)
            except (AgentUnavailable, AgentError) as exc:
                now = asyncio.get_running_loop().time()
                unreachable_since = unreachable_since or now
                waited = now - unreachable_since

                if waited > grace:
                    # The vzdump may well have finished. Recording it as failed would be a
                    # lie, and retrying it could start a second one — `interrupted` says
                    # exactly what is true: the outcome is unknown.
                    await self._interrupt_backup(
                        backup_id,
                        f"The agent stopped answering for {int(waited)}s while this backup "
                        f"was running ({exc.detail}). Its outcome is unknown — check the "
                        "host before retrying.",
                    )
                    return None

                logger.warning(
                    "backup_poll_failed",
                    backup_id=backup_id,
                    agent_task_id=task_id,
                    waited_seconds=round(waited, 1),
                    error=str(exc.detail),
                )
                await self._sleep(min(interval * 2, 30))
                continue

            unreachable_since = None
            if task.is_terminal:
                return task

            self._publish_progress(run_id=run_id, backup_id=backup_id, target=target, task=task)
            await self._sleep(interval)

        return None

    def _publish_progress(
        self, *, run_id: int, backup_id: int, target: ResolvedTarget, task: AgentTask
    ) -> None:
        progress = RunProgress(
            backup_id=backup_id,
            vmid=target.vmid,
            guest_name=target.guest_name,
            percent=task.progress.percent,
            bytes_done=task.progress.bytes_done,
            bytes_total=task.progress.bytes_total,
            rate_bps=task.progress.rate_bps,
            eta_seconds=task.progress.eta_seconds,
            message=task.progress.message,
        )
        self._progress[run_id] = progress
        self._events.publish(
            EVENT_BACKUP_PROGRESS, run_id=run_id, **progress.model_dump(exclude_none=True)
        )

    async def _record_outcome(
        self, *, backup_id: int, run_id: int, task: AgentTask, upload: bool
    ) -> None:
        status = _AGENT_STATE_TO_BACKUP_STATUS.get(task.state, BackupStatus.FAILED)
        result = task.result

        # Becoming successful makes a copy visible to retention. Serialize that transition
        # through commit with retention's final classify/delete lane.
        async with self._retention_guard.hold(), self._database.session() as session:
            history = SqlAlchemyBackupHistoryRepository(session)
            record = await history.get(backup_id)
            if record is None:  # pragma: no cover - the row was created by this worker
                return

            record.status = status.value
            record.finished_at = task.finished_at or datetime.now(UTC)
            record.duration_seconds = task.duration_seconds
            record.log_path = task.log_path
            record.error_message = task.error

            if status is BackupStatus.SUCCESS:
                _apply_artifact(record, result)
                if not upload:
                    record.upload_status = UploadStatus.NOT_REQUIRED.value
            else:
                # Nothing to upload, whatever the job asked for.
                record.upload_status = UploadStatus.NOT_REQUIRED.value

            await self._bump_counters(session, run_id, succeeded=status is BackupStatus.SUCCESS)
            payload = _backup_payload(record)

        logger.info(
            "backup_finished",
            backup_id=backup_id,
            run_id=run_id,
            status=status.value,
            filename=payload.get("filename"),
            size_bytes=payload.get("size_bytes"),
            duration_seconds=task.duration_seconds,
        )
        # `payload` already carries run_id; passing it again would be a duplicate keyword.
        self._events.publish(EVENT_BACKUP_STATE, **payload)
        if status is BackupStatus.SUCCESS and not upload and self._retention is not None:
            # `_record_outcome`'s transaction has committed before this line. Retention must
            # never observe eligibility early and delete a file whose success row rolls back.
            self._retention.notify(backup_id)

    async def _bump_counters(self, session: AsyncSession, run_id: int, *, succeeded: bool) -> None:
        run = await SqlAlchemyBackupRunRepository(session).get(run_id)
        if run is None:  # pragma: no cover
            return
        if succeeded:
            run.guest_succeeded += 1
        else:
            run.guest_failed += 1

    async def _fail_backup(self, backup_id: int, message: str) -> None:
        await self._close_backup(backup_id, BackupStatus.FAILED, message)

    async def _interrupt_backup(self, backup_id: int, message: str) -> None:
        await self._close_backup(backup_id, BackupStatus.INTERRUPTED, message)

    async def _close_backup(self, backup_id: int, status: BackupStatus, message: str) -> None:
        async with self._database.session() as session:
            history = SqlAlchemyBackupHistoryRepository(session)
            record = await history.get(backup_id)
            if record is None:  # pragma: no cover
                return
            record.status = status.value
            record.error_message = message
            record.finished_at = datetime.now(UTC)
            record.upload_status = UploadStatus.NOT_REQUIRED.value
            if record.started_at is not None:
                record.duration_seconds = round(
                    (record.finished_at - record.started_at).total_seconds(), 2
                )
            run_id = record.run_id
            if run_id is not None:
                await self._bump_counters(session, run_id, succeeded=False)
            payload = _backup_payload(record)

        logger.warning("backup_not_completed", backup_id=backup_id, status=status.value)
        self._events.publish(EVENT_BACKUP_STATE, **payload)

    async def _finalise_run(self, run_id: int, *, error: str | None = None) -> None:
        async with self._database.session() as session:
            runs = SqlAlchemyBackupRunRepository(session)
            run = await runs.get(run_id)
            if run is None:  # pragma: no cover
                return

            records = await SqlAlchemyBackupHistoryRepository(session).for_run(run_id)
            status = (
                RunStatus.CANCELLED
                if run_id in self._cancelled
                else RunStatus.FAILED
                if error
                else summarise_run(run, records)
            )

            run.status = status.value
            run.finished_at = datetime.now(UTC)
            if run.started_at is not None:
                run.duration_seconds = round((run.finished_at - run.started_at).total_seconds(), 2)
            run.error_message = error or run.error_message
            payload = run_payload(run)
            await self._notify_run_finished(session, run_id, status=status, records=records)

        self._cancelled.discard(run_id)
        self._progress.pop(run_id, None)
        logger.info("backup_run_finished", **payload)
        self._events.publish(EVENT_RUN_STATE, **payload)

    async def _notify_run_finished(
        self,
        session: AsyncSession,
        run_id: int,
        *,
        status: RunStatus,
        records: list[BackupHistory],
    ) -> None:
        """Announce the run's outcome, once, in the transaction that records it.

        A cancelled run is deliberately silent: the operator who stopped it already knows, and
        a "backup failed" alert for something they did themselves is the fastest way to teach
        them to ignore alerts.
        """
        if self._notifications is None or status is RunStatus.CANCELLED:
            return

        succeeded = [record for record in records if record.status == BackupStatus.SUCCESS.value]
        failed = [record for record in records if record.status != BackupStatus.SUCCESS.value]
        run = await SqlAlchemyBackupRunRepository(session).get(run_id)
        variables: dict[str, Any] = {
            "run_id": run_id,
            "guest_total": run.guest_total if run is not None else len(records),
            "succeeded": len(succeeded),
            "duration_seconds": run.duration_seconds if run is not None else None,
            "total_bytes": sum(record.size_bytes or 0 for record in succeeded),
            "failed_guests": [
                {
                    "guest_name": record.guest_name,
                    "vmid": record.vmid,
                    "guest_type": record.guest_type,
                }
                for record in failed
            ],
            "error": next(
                (record.error_message for record in failed if record.error_message),
                run.error_message if run is not None else None,
            ),
        }

        # `partial` is reported as a failure: at least one guest has no backup, which is what
        # an operator has to act on. The message says how many succeeded.
        event = (
            NotificationEvent.BACKUP_SUCCESS
            if status is RunStatus.SUCCESS
            else NotificationEvent.BACKUP_FAILED
        )
        await self._notifications.emit(
            session,
            event,
            dedupe_key=f"run:{run_id}:{event.value}",
            unique=True,
            variables=variables,
        )

    # ---- cancellation --------------------------------------------------------

    async def cancel(self, run_id: int) -> bool:
        """Stop a run in flight. Returns whether anything was actually cancelled.

        The guest currently being backed up is cancelled at the agent; guests not yet started
        are simply never started. Artifacts already written stay — a completed backup is not
        undone because the operator stopped the ones after it.
        """
        self._cancelled.add(run_id)
        if run_id != self._current_run_id or self._current_task_id is None:
            return False

        task_id = self._current_task_id
        try:
            await self._agent.cancel_task(task_id)
        except (AgentUnavailable, AgentError) as exc:
            logger.warning("backup_cancel_failed", run_id=run_id, task_id=task_id, error=str(exc))
            return False
        logger.info("backup_cancel_requested", run_id=run_id, agent_task_id=task_id)
        return True

    # ---- recovery ------------------------------------------------------------

    async def recover(self) -> None:
        """Reconcile whatever the previous process left behind.

        Three cases, and each has exactly one honest answer:

        * A row with no agent task id — we died before the agent was asked. Nothing was
          started, but a lost *response* looks identical from here, so the row becomes
          `interrupted` rather than being retried. Never risk a second vzdump.
        * A row whose task the agent still knows and has finished — adopt the real outcome.
        * A row whose task is still running — leave it; the queue picks the run back up and
          `_execute_run` re-adopts the task instead of starting a new one.
        """
        pending, adoptable = await self._orphaned_backups()

        for backup_id in pending:
            await self._interrupt_backup(
                backup_id,
                "The dashboard restarted before the agent confirmed this backup. It was not "
                "retried automatically, because the request may still have reached the host. "
                "Check the node, then run it again if needed.",
            )

        for backup_id, task_id in adoptable:
            await self._recover_task(backup_id, task_id)

        await self._requeue_runs()

    async def _orphaned_backups(self) -> tuple[list[int], list[tuple[int, str]]]:
        """Split rows left in `running` into "never confirmed" and "the agent may know"."""
        async with self._database.session() as session:
            history = SqlAlchemyBackupHistoryRepository(session)
            records = await history.search(
                status=BackupStatus.RUNNING, limit=RECOVERY_SCAN_LIMIT, include_deleted=True
            )

        pending = [record.id for record in records if record.agent_task_id is None]
        adoptable = [
            (record.id, record.agent_task_id)
            for record in records
            if record.agent_task_id is not None
        ]
        if pending or adoptable:
            logger.info(
                "backup_recovery_scan",
                unconfirmed=len(pending),
                adoptable=len(adoptable),
            )
        return pending, adoptable

    async def _recover_task(self, backup_id: int, task_id: str) -> None:
        try:
            task = await self._agent.get_task(task_id)
        except AgentError as exc:
            if exc.agent_status == 404:
                await self._interrupt_backup(
                    backup_id,
                    "The agent no longer has a record of this task, so its outcome cannot be "
                    "established. Check the node's dump directory before running it again.",
                )
            else:
                logger.warning("backup_recovery_failed", backup_id=backup_id, error=str(exc.detail))
            return
        except AgentUnavailable:
            # The agent may simply be starting more slowly than we are. Leave the row alone:
            # the run executor re-adopts the task when it picks the run back up.
            logger.warning("backup_recovery_deferred", backup_id=backup_id, task_id=task_id)
            return

        if not task.is_terminal:
            logger.info("backup_still_running_on_agent", backup_id=backup_id, task_id=task_id)
            return

        run_id, upload = await self._context_for(backup_id)
        await self._record_outcome(backup_id=backup_id, run_id=run_id, task=task, upload=upload)

    async def _context_for(self, backup_id: int) -> tuple[int, bool]:
        """The run a recovered backup belonged to, and whether it was meant to be uploaded."""
        async with self._database.session() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
            run = (
                await SqlAlchemyBackupRunRepository(session).get(record.run_id)
                if record is not None and record.run_id is not None
                else None
            )
            if run is None:
                return 0, False
            return run.id, RunOptions.model_validate(run.options or {}).upload

    async def _requeue_runs(self) -> None:
        """Put interrupted runs back on the queue so their remaining guests are attempted."""
        async with self._database.session() as session:
            runs = SqlAlchemyBackupRunRepository(session)
            for run in await runs.active():
                if run.status == RunStatus.RUNNING.value:
                    run.status = RunStatus.QUEUED.value
                    logger.info("backup_run_requeued", run_id=run.id)
        self.notify()

    async def _poll_interval(self) -> float:
        async with self._database.session() as session:
            service = SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=self._secret_box,
            )
            section = await service.get_section(SettingsSection.AGENT)
        assert isinstance(section, AgentSettings)
        return float(section.poll_interval_seconds)


def _apply_artifact(record: BackupHistory, result: dict[str, Any]) -> None:
    """Copy the agent's task result onto the history row.

    Only `filename` is required for the row to be useful; everything else is best-effort,
    because a successful vzdump whose checksum could not be computed is still a real backup
    and must not be recorded as a failure.
    """
    filename = result.get("filename")
    if isinstance(filename, str):
        record.filename = filename
    path = result.get("path")
    if isinstance(path, str):
        record.local_path = path
    size = result.get("size_bytes")
    if isinstance(size, int):
        record.size_bytes = size
    checksum = result.get("checksum_sha256")
    if isinstance(checksum, str):
        record.checksum_sha256 = checksum
    warnings = result.get("warnings")
    if isinstance(warnings, list):
        record.warnings = [str(item) for item in warnings]


def _backup_payload(record: BackupHistory) -> dict[str, Any]:
    return {
        "backup_id": record.id,
        "run_id": record.run_id,
        "vmid": record.vmid,
        "guest_name": record.guest_name,
        "status": record.status,
        "filename": record.filename,
        "size_bytes": record.size_bytes,
        "duration_seconds": record.duration_seconds,
        "error": record.error_message,
    }
