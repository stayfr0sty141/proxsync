"""The restore executor: the only component that drives a confirmed restore onto the host.

**The database is the queue**, as it is for backups and transfers. A confirmed restore is
durable before anything is started, so a crash between confirmation and execution loses
nothing and restart recovery is a query rather than a guess.

**A restore is never retried automatically, and never re-issued.** `qmrestore` and
`pct restore` destroy the target and rebuild it. Re-sending a request whose response was lost
could run a second one over a restore still in progress, so where the outcome is unknown the
row becomes `interrupted` and waits for a human. That is the same rule the run executor
applies to `vzdump`, for a strictly worse failure mode.

**Restores are serialised globally.** The agent holds one restore slot; this worker claims one
row at a time, and `RestoreService` refuses to confirm a second while one is authorised.

A Drive-only backup is downloaded first, in the same task, before the restore is started —
the download is a prerequisite of this restore, not an independent transfer, and queueing it
on the sync worker would leave the restore waiting on a queue it does not control.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import AgentError, AgentUnavailable, AppError
from app.core.events import EVENT_RESTORE_PROGRESS, EVENT_RESTORE_STATE, EventBus
from app.core.logging import logger, set_correlation_id
from app.core.retention_guard import RetentionGuard
from app.db.models.backup import RestoreHistory
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.agent import AgentTask
from app.schemas.enums import (
    GuestType,
    NotificationEvent,
    RestoreSource,
    RestoreStatus,
    SettingsSection,
)
from app.schemas.settings import AgentSettings, GDriveSettings
from app.services.notification_service import NotificationGateway
from app.services.settings_service import SettingsService

_AGENT_STATE_TO_RESTORE_STATUS = {
    "success": RestoreStatus.SUCCESS,
    "failed": RestoreStatus.FAILED,
    "cancelled": RestoreStatus.CANCELLED,
    "interrupted": RestoreStatus.INTERRUPTED,
}


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """A confirmed restore, read out of the database into the shape the executor works in."""

    restore_id: int
    filename: str
    guest_type: GuestType
    target_vmid: int
    storage: str
    overwrite: bool
    force_stop: bool
    start_after: bool
    source: RestoreSource
    expected_sha256: str | None
    backup_id: int
    correlation_id: str


class RestoreWorker:
    def __init__(
        self,
        *,
        database: Database,
        agent: AgentClient,
        events: EventBus,
        secret_box: SecretBox,
        settings: Settings,
        retention_guard: RetentionGuard | None = None,
        notifications: NotificationGateway | None = None,
    ) -> None:
        self._database = database
        self._agent = agent
        self._events = events
        self._secret_box = secret_box
        self._settings = settings
        self._retention_guard = retention_guard or RetentionGuard()
        self._notifications = notifications

        self._task: asyncio.Task[None] | None = None
        self._doorbell = asyncio.Event()
        self._stopping = asyncio.Event()
        self._current_restore_id: int | None = None
        self._current_agent_task: str | None = None
        self._resume: list[tuple[int, str]] = []

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.recover()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="proxsync-restore-worker")
        logger.info("restore_worker_started")

    async def stop(self) -> None:
        self._stopping.set()
        self._doorbell.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._task, timeout=10)
            self._task = None
        logger.info("restore_worker_stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_restore_id(self) -> int | None:
        return self._current_restore_id

    def notify(self) -> None:
        self._doorbell.set()

    def notify_after_commit(self, session: AsyncSession) -> None:
        """Ring the doorbell once the transaction that confirmed the restore commits.

        Calling `notify()` from the handler would race the request's own commit: the worker
        could query before the `confirmed` row was visible and go back to sleep.
        """

        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _ring(_session: Session) -> None:
            self._doorbell.set()

    # ---- queue ---------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            # Cleared before claiming, so a notify arriving *during* a claim is not lost.
            self._doorbell.clear()
            try:
                await self._expire_stale()
                claim = await self._claim_next()
            except Exception:  # noqa: BLE001 - the worker must outlive a bad iteration
                logger.error("restore_worker_claim_failed", exc_info=True)
                await self._sleep(self._settings.restore_idle_poll_seconds)
                continue

            if claim is None:
                await self._wait_for_work()
                continue

            restore_id, adopted_task = claim
            try:
                await self._execute(restore_id, adopted_task=adopted_task)
            except Exception:  # noqa: BLE001 - one broken restore must not stop the queue
                logger.error("restore_crashed", restore_id=restore_id, exc_info=True)
                await self._close(
                    restore_id,
                    RestoreStatus.INTERRUPTED,
                    "The restore executor failed unexpectedly. The target guest may be in a "
                    "partial state — check the node before running this again.",
                )
            finally:
                self._current_restore_id = None
                self._current_agent_task = None

    async def _wait_for_work(self) -> None:
        """Sleep until the doorbell rings, or the idle poll comes round.

        The poll is the backstop for a doorbell rung a moment before its transaction
        committed, and it is what drives the confirmation-expiry sweep.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._doorbell.wait(), timeout=self._settings.restore_idle_poll_seconds
            )

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _claim_next(self) -> tuple[int, str | None] | None:
        """Adopt a restore left running by the previous process, or claim a confirmed one.

        Adoption comes first: a restore already on the host has to be followed to its real end
        before another is allowed to start, because the agent holds exactly one restore slot.

        The claim shares the retention guard with the guarded routes, which is what stops a
        cancel from landing on a restore this worker claimed a moment earlier. Without it the
        row could read `cancelled` while the agent was rewriting the guest — briefly, since the
        outcome overwrites it, but "we stopped it" is the one thing it must never say wrongly.
        """
        if self._resume:
            return self._resume.pop(0)

        async with self._retention_guard.hold(), self._database.session() as session:
            restores = SqlAlchemyRestoreRepository(session)
            record = await restores.next_confirmed()
            if record is None:
                return None
            record.status = RestoreStatus.RUNNING.value
            if record.started_at is None:
                record.started_at = datetime.now(UTC)
            await restores.flush()
            await self._notify_started(session, record)
            return record.id, None

    async def _notify_started(self, session: AsyncSession, record: RestoreHistory) -> None:
        """Announce the restore in the transaction that makes it `running`.

        Not on the adoption path: a restore picked back up after a restart was announced when
        it first started, and the unique key would refuse a second message anyway.
        """
        if self._notifications is None:
            return
        backup = await SqlAlchemyBackupHistoryRepository(session).get(record.backup_id)
        await self._notifications.emit(
            session,
            NotificationEvent.RESTORE_STARTED,
            dedupe_key=f"restore:{record.id}:started",
            unique=True,
            restore_id=record.id,
            backup_id=record.backup_id,
            variables={
                "restore_id": record.id,
                "filename": backup.filename if backup is not None else None,
                "target_vmid": record.target_vmid,
                "target_type": record.target_type,
                "target_storage": record.target_storage,
                "source": record.source,
                "overwrite": record.overwrite_existing,
            },
        )

    async def _expire_stale(self) -> None:
        """Close abandoned confirmation windows so they stop blocking retention."""
        async with self._retention_guard.hold(), self._database.session() as session:
            ids = await SqlAlchemyRestoreRepository(session).expire_pending(now=datetime.now(UTC))

        for restore_id in ids:
            logger.info("restore_expired", restore_id=restore_id)
            self._events.publish(
                EVENT_RESTORE_STATE, restore_id=restore_id, status=RestoreStatus.EXPIRED.value
            )

    # ---- execution -----------------------------------------------------------

    async def _execute(self, restore_id: int, *, adopted_task: str | None = None) -> None:
        self._current_restore_id = restore_id

        if adopted_task is not None:
            # Already on the host. Nothing is started; the task is simply followed to its end.
            self._current_agent_task = adopted_task
            set_correlation_id(await self._correlation_for(restore_id))
            logger.info("restore_readopted", restore_id=restore_id, agent_task_id=adopted_task)
            await self._follow(restore_id, adopted_task)
            return

        plan, problem = await self._load(restore_id)
        if plan is None:
            if problem is not None:
                await self._close(restore_id, RestoreStatus.FAILED, problem)
            return

        set_correlation_id(plan.correlation_id or None)
        self._events.publish(
            EVENT_RESTORE_STATE,
            restore_id=restore_id,
            backup_id=plan.backup_id,
            target_vmid=plan.target_vmid,
            status=RestoreStatus.RUNNING.value,
        )
        logger.info(
            "restore_started",
            restore_id=restore_id,
            backup_id=plan.backup_id,
            filename=plan.filename,
            target_vmid=plan.target_vmid,
            guest_type=plan.guest_type.value,
            source=plan.source.value,
        )

        if plan.source is RestoreSource.GDRIVE and not await self._download(plan):
            return

        try:
            accepted = await self._agent.start_restore(
                guest_type=plan.guest_type,
                archive=plan.filename,
                target_vmid=plan.target_vmid,
                storage=plan.storage,
                overwrite=plan.overwrite,
                force_stop=plan.force_stop,
                start_after=plan.start_after,
                expected_sha256=plan.expected_sha256,
            )
        except AgentUnavailable as exc:
            # A lost *response* is indistinguishable from a lost *request*: the agent may have
            # accepted this and be restoring right now. POSTs are never retried for exactly
            # this reason, and the row must not claim the restore did not happen.
            await self._close(
                restore_id,
                RestoreStatus.INTERRUPTED,
                f"The agent could not be reached while starting this restore ({exc.detail}). "
                "The request may still have arrived — check the guest on the node before "
                "running it again.",
            )
            return
        except (AgentError, AppError) as exc:
            # The agent answered and refused, so nothing on the host changed.
            await self._close(restore_id, RestoreStatus.FAILED, str(exc.detail))
            return

        await self._attach_task(restore_id, accepted.task_id)
        self._current_agent_task = accepted.task_id
        await self._follow(restore_id, accepted.task_id)

    async def _follow(self, restore_id: int, agent_task_id: str) -> None:
        """Poll one agent task to a terminal state and record what it says."""
        task = await self._poll(agent_task_id, restore_id=restore_id)
        if task is None:
            return  # shutdown, or already closed as interrupted by the poll loop
        await self._record_outcome(restore_id, task)

    async def _load(self, restore_id: int) -> tuple[RestorePlan | None, str | None]:
        """Read the plan, or the reason there is not one. Never mutates."""
        async with self._database.session() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            if record is None:  # pragma: no cover - the row was just claimed
                logger.error("restore_vanished", restore_id=restore_id)
                return None, None

            backup = await SqlAlchemyBackupHistoryRepository(session).get(record.backup_id)
            if backup is None or backup.filename is None:
                return None, "The backup this restore refers to no longer has an artifact."

            plan = RestorePlan(
                restore_id=record.id,
                filename=backup.filename,
                guest_type=GuestType(record.target_type),
                target_vmid=record.target_vmid,
                storage=record.target_storage,
                overwrite=record.overwrite_existing,
                force_stop=record.force_stop,
                start_after=record.start_after_restore,
                source=RestoreSource(record.source),
                expected_sha256=backup.checksum_sha256,
                backup_id=backup.id,
                correlation_id=record.correlation_id,
            )
        return plan, None

    # ---- download-first ------------------------------------------------------

    async def _download(self, plan: RestorePlan) -> bool:
        """Pull a Drive-only archive back to the host. Returns whether the restore may go on."""
        config = await self._gdrive_config()
        if not config.enabled:
            await self._close(
                plan.restore_id,
                RestoreStatus.FAILED,
                "This backup exists only on Google Drive, but Drive sync is disabled, so the "
                "archive cannot be fetched.",
            )
            return False

        logger.info(
            "restore_download_started",
            restore_id=plan.restore_id,
            filename=plan.filename,
            remote=config.remote_name,
        )
        self._events.publish(
            EVENT_RESTORE_PROGRESS,
            restore_id=plan.restore_id,
            phase="download",
            message=f"Fetching {plan.filename} from {config.remote_name}",
        )

        try:
            accepted = await self._agent.start_download(
                filename=plan.filename,
                remote=config.remote_name,
                remote_path=config.folder,
                bwlimit_kbps=config.bandwidth_limit_kbps,
            )
        except (AgentUnavailable, AgentError, AppError) as exc:
            await self._close(plan.restore_id, RestoreStatus.FAILED, str(exc.detail))
            return False

        task = await self._poll(accepted.task_id, restore_id=plan.restore_id, phase="download")
        if task is None:
            return False
        if not task.succeeded:
            await self._close(
                plan.restore_id,
                RestoreStatus.FAILED,
                task.error or f"The download ended in state '{task.state}'",
            )
            return False

        await self._mark_local_copy_present(plan.backup_id)
        logger.info(
            "restore_download_finished", restore_id=plan.restore_id, backup_id=plan.backup_id
        )
        return True

    async def _mark_local_copy_present(self, backup_id: int) -> None:
        """The artifact is on the host again, so the row must stop claiming it is not.

        Held under the retention guard: `local_deleted_at` is one of the facts retention ranks
        copies by, and a half-applied change would let it act on a copy state that never
        existed.
        """
        async with self._retention_guard.hold(), self._database.session() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
            if record is not None:
                record.local_deleted_at = None

    # ---- polling -------------------------------------------------------------

    async def _poll(
        self, agent_task_id: str, *, restore_id: int, phase: str = "restore"
    ) -> AgentTask | None:
        interval = await self._poll_interval()
        unreachable_since: float | None = None
        grace = self._settings.agent_unreachable_grace_seconds

        while not self._stopping.is_set():
            try:
                task = await self._agent.get_task(agent_task_id)
            except (AgentUnavailable, AgentError) as exc:
                if isinstance(exc, AgentError) and exc.agent_status == 404:
                    # The agent has no record of the task. Whether it ran is unknowable from
                    # here, and a restore is not something to guess about.
                    await self._close(
                        restore_id,
                        RestoreStatus.INTERRUPTED,
                        "The agent no longer has a record of this task, so the restore's "
                        "outcome cannot be established. Check the guest on the node before "
                        "running it again.",
                    )
                    return None

                now = asyncio.get_running_loop().time()
                unreachable_since = unreachable_since or now
                waited = now - unreachable_since
                if waited > grace:
                    # The restore may well have completed. Recording it as failed would be a
                    # lie an operator could act on by running it again, over a guest that is
                    # already being rebuilt.
                    await self._close(
                        restore_id,
                        RestoreStatus.INTERRUPTED,
                        f"The agent stopped answering for {int(waited)}s while this restore "
                        f"was running ({exc.detail}). Its outcome is unknown — check the "
                        "guest on the node before running it again.",
                    )
                    return None

                logger.warning(
                    "restore_poll_failed",
                    restore_id=restore_id,
                    agent_task_id=agent_task_id,
                    phase=phase,
                    waited_seconds=round(waited, 1),
                    error=str(exc.detail),
                )
                await self._sleep(min(interval * 2, 30))
                continue

            unreachable_since = None
            if task.is_terminal:
                return task

            self._publish_progress(restore_id, task, phase=phase)
            await self._sleep(interval)

        return None

    async def _correlation_for(self, restore_id: int) -> str | None:
        async with self._database.session() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            return record.correlation_id or None if record is not None else None

    def _publish_progress(self, restore_id: int, task: AgentTask, *, phase: str) -> None:
        progress = task.progress
        self._events.publish(
            EVENT_RESTORE_PROGRESS,
            restore_id=restore_id,
            phase=phase,
            percent=progress.percent,
            bytes_done=progress.bytes_done,
            bytes_total=progress.bytes_total,
            rate_bps=progress.rate_bps,
            eta_seconds=progress.eta_seconds,
            message=progress.message,
        )

    # ---- outcome -------------------------------------------------------------

    async def _attach_task(self, restore_id: int, agent_task_id: str) -> None:
        async with self._database.session() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            if record is not None:
                record.agent_task_id = agent_task_id

    async def _record_outcome(self, restore_id: int, task: AgentTask) -> None:
        status = _AGENT_STATE_TO_RESTORE_STATUS.get(task.state, RestoreStatus.FAILED)
        warnings = task.result.get("warnings")

        # The row leaves its retention-blocking states here, so the transition shares a commit
        # boundary with retention's classify/delete lane.
        async with self._retention_guard.hold(), self._database.session() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            if record is None:  # pragma: no cover - claimed by this worker
                return
            record.status = status.value
            record.finished_at = task.finished_at or datetime.now(UTC)
            record.duration_seconds = task.duration_seconds
            record.log_path = task.log_path
            record.error_message = task.error or _warning_text(warnings)
            payload = _restore_payload(record)
            await self._notify_finished(session, record)

        log = logger.info if status is RestoreStatus.SUCCESS else logger.warning
        log(
            "restore_finished",
            restore_id=restore_id,
            status=status.value,
            duration_seconds=task.duration_seconds,
            error=task.error,
        )
        self._events.publish(EVENT_RESTORE_STATE, **payload)

    async def _close(self, restore_id: int, status: RestoreStatus, message: str) -> None:
        async with self._retention_guard.hold(), self._database.session() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            if record is None:  # pragma: no cover
                return
            record.status = status.value
            record.error_message = message
            record.finished_at = datetime.now(UTC)
            if record.started_at is not None:
                record.duration_seconds = round(
                    (record.finished_at - record.started_at).total_seconds(), 2
                )
            payload = _restore_payload(record)
            await self._notify_finished(session, record)

        logger.warning("restore_not_completed", restore_id=restore_id, status=status.value)
        self._events.publish(EVENT_RESTORE_STATE, **payload)

    async def _notify_finished(self, session: AsyncSession, record: RestoreHistory) -> None:
        """Announce a terminal restore, once, in the transaction that records it.

        A cancelled restore is silent for the same reason a cancelled backup is: the operator
        who stopped it does not need to be told, and `error_message` already carries the
        agent's warning about the partial guest it left behind.
        """
        if self._notifications is None:
            return
        status = RestoreStatus(record.status)
        if status is RestoreStatus.CANCELLED:
            return

        succeeded = status is RestoreStatus.SUCCESS
        event = (
            NotificationEvent.RESTORE_FINISHED if succeeded else NotificationEvent.RESTORE_FAILED
        )
        await self._notifications.emit(
            session,
            event,
            dedupe_key=f"restore:{record.id}:{event.value}",
            unique=True,
            restore_id=record.id,
            backup_id=record.backup_id,
            variables={
                "restore_id": record.id,
                "target_vmid": record.target_vmid,
                "target_type": record.target_type,
                "duration_seconds": record.duration_seconds,
                "status": status.value,
                # On success `error_message` holds the agent's warnings — a guest that would
                # not start is a real outcome an operator has to see, not an error.
                "warning": record.error_message if succeeded else None,
                "error": None if succeeded else record.error_message,
            },
        )

    # ---- cancellation --------------------------------------------------------

    async def cancel(self, restore_id: int) -> bool:
        """Ask the agent to stop the restore it is running. Returns whether it was asked.

        There is no "cancel a queued restore" here — the service does that, because a restore
        that has not reached the host is cancelled by a state change and nothing else. A
        cancelled restore leaves the target guest in a partial state; the agent says so, and
        that message reaches the row through the normal outcome path.
        """
        if restore_id != self._current_restore_id or self._current_agent_task is None:
            return False

        task_id = self._current_agent_task
        try:
            await self._agent.cancel_task(task_id)
        except (AgentUnavailable, AgentError) as exc:
            logger.warning(
                "restore_cancel_failed", restore_id=restore_id, task_id=task_id, error=str(exc)
            )
            return False
        logger.info("restore_cancel_requested", restore_id=restore_id, agent_task_id=task_id)
        return True

    # ---- recovery ------------------------------------------------------------

    async def recover(self) -> None:
        """Reconcile whatever the previous process left in `running`.

        Two cases, one honest answer each:

        * No agent task id — we died before the agent confirmed it. The request may still have
          reached the host, so the row becomes `interrupted`. It is never re-issued: a second
          `qmrestore` over a running one is the worst outcome available here.
        * An agent task id — the agent knows what happened, or is still doing it. The task is
          adopted and followed to its real end, exactly as if it had never been interrupted.
        """
        async with self._database.session() as session:
            records = await SqlAlchemyRestoreRepository(session).running()
            unconfirmed = [record.id for record in records if record.agent_task_id is None]
            adoptable = [
                (record.id, record.agent_task_id)
                for record in records
                if record.agent_task_id is not None
            ]

        for restore_id in unconfirmed:
            await self._close(
                restore_id,
                RestoreStatus.INTERRUPTED,
                "The dashboard restarted before the agent confirmed this restore. It was not "
                "retried automatically, because the request may still have reached the host. "
                "Check the guest on the node before running it again.",
            )

        self._resume = adoptable
        if unconfirmed or adoptable:
            logger.info(
                "restore_recovery_scan", unconfirmed=len(unconfirmed), adopted=len(adoptable)
            )
        self.notify()

    # ---- settings ------------------------------------------------------------

    async def _gdrive_config(self) -> GDriveSettings:
        async with self._database.session() as session:
            section = await self._settings_service(session).get_section(SettingsSection.GDRIVE)
        assert isinstance(section, GDriveSettings)
        return section

    async def _poll_interval(self) -> float:
        async with self._database.session() as session:
            section = await self._settings_service(session).get_section(SettingsSection.AGENT)
        assert isinstance(section, AgentSettings)
        return float(section.poll_interval_seconds)

    def _settings_service(self, session: AsyncSession) -> SettingsService:
        return SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
        )


def _warning_text(warnings: object) -> str | None:
    """A successful restore whose guest failed to start is still a success — but the operator
    has to be told, and `error_message` is the only field the UI surfaces for that."""
    if isinstance(warnings, list) and warnings:
        return "; ".join(str(item) for item in warnings)[:2000]
    return None


def _restore_payload(record: RestoreHistory) -> dict[str, object]:
    return {
        "restore_id": record.id,
        "backup_id": record.backup_id,
        "target_vmid": record.target_vmid,
        "target_type": record.target_type,
        "status": record.status,
        "duration_seconds": record.duration_seconds,
        "error": record.error_message,
    }
