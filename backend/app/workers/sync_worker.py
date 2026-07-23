"""The upload queue: moves artifacts to Google Drive and records what happened.

Same shape as the run executor, for the same reasons. **The database is the queue** —
`sync_tasks` holds `attempt`, `max_attempts` and `next_retry_at`, so a restart resumes the
backoff schedule rather than restarting it, and a transfer interrupted by a crash is picked
back up instead of being forgotten.

**A retry is safe here in a way it is not for backups.** Re-running `rclone copyto` with the
same source and destination overwrites the same object; it cannot produce a second artifact
the way a second `vzdump` would. That is what makes an automatic retry policy defensible at
all, and it is why `--retries 1` is passed to rclone: the backoff, the attempt count and the
record of each failure belong here, where an operator can see them.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.clients.agent_client import AgentClient
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import AgentError, AgentUnavailable, AppError
from app.core.events import EventBus
from app.core.logging import logger, set_correlation_id
from app.core.retention_guard import RetentionGuard
from app.db.models.backup import BackupHistory
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.sync_task_repository import SqlAlchemySyncTaskRepository
from app.schemas.agent import AgentTask
from app.schemas.enums import NotificationEvent, SettingsSection, SyncStatus, UploadStatus
from app.schemas.settings import AgentSettings, GDriveSettings
from app.services.notification_service import NotificationGateway
from app.services.settings_service import SettingsService
from app.services.sync_service import SyncService

EVENT_SYNC_PROGRESS = "sync.progress"
EVENT_SYNC_STATE = "sync.state"


class RetentionNotifier(Protocol):
    """A successful upload only needs to ring retention's durable-state rescan."""

    def notify(self, backup_id: int) -> None: ...


class SyncWorker:
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
        self._current_task_id: int | None = None
        self._current_agent_task: str | None = None

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.recover()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="proxsync-sync-worker")
        logger.info("sync_worker_started")

    async def stop(self) -> None:
        self._stopping.set()
        self._doorbell.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10)
            self._task = None
        logger.info("sync_worker_stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_task_id(self) -> int | None:
        return self._current_task_id

    def notify(self) -> None:
        self._doorbell.set()

    def notify_after_commit(self, session: AsyncSession) -> None:
        """Ring the doorbell when the transaction that queued the transfer commits."""

        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _ring(_session: Session) -> None:
            self._doorbell.set()

    # ---- queue ---------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            self._doorbell.clear()
            try:
                await self._enqueue_pending()
                task_id = await self._claim_next()
            except Exception:  # noqa: BLE001 - the worker must outlive a bad iteration
                logger.error("sync_worker_claim_failed", exc_info=True)
                await self._sleep(self._settings.sync_idle_poll_seconds)
                continue

            if task_id is None:
                await self._wait_for_work()
                continue

            try:
                await self._execute(task_id)
            except Exception:  # noqa: BLE001 - one broken transfer must not stop the queue
                logger.error("sync_task_crashed", sync_task_id=task_id, exc_info=True)
                await self._record_failure(task_id, "The sync worker failed unexpectedly")
            finally:
                self._current_task_id = None
                self._current_agent_task = None

    async def _wait_for_work(self) -> None:
        """Sleep until the doorbell rings or the idle poll comes round.

        The poll also drives the retry schedule: a transfer waiting on backoff becomes ready
        without anything ringing a bell for it.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._doorbell.wait(), timeout=self._settings.sync_idle_poll_seconds
            )

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _enqueue_pending(self) -> None:
        async with self._database.session() as session:
            await self._service(session).enqueue_pending()

    async def _claim_next(self) -> int | None:
        async with self._database.session() as session:
            tasks = SqlAlchemySyncTaskRepository(session)
            task = await tasks.next_ready(now=datetime.now(UTC))
            if task is None:
                return None
            task.status = SyncStatus.RUNNING.value
            task.attempt += 1
            task.next_retry_at = None
            if task.started_at is None:
                task.started_at = datetime.now(UTC)
            await tasks.flush()
            return task.id

    # ---- execution -----------------------------------------------------------

    async def _execute(self, task_id: int) -> None:
        plan = await self._load(task_id)
        if plan is None:
            return

        filename, remote, remote_path, correlation_id, attempt = plan
        set_correlation_id(correlation_id or None)
        self._current_task_id = task_id

        config = await self._gdrive_config()
        self._events.publish(
            EVENT_SYNC_STATE, sync_task_id=task_id, status=SyncStatus.RUNNING.value
        )
        logger.info(
            "sync_transfer_started",
            sync_task_id=task_id,
            filename=filename,
            remote=remote,
            attempt=attempt,
        )

        try:
            accepted = await self._agent.start_upload(
                filename=filename,
                remote=remote,
                remote_path=remote_path,
                bwlimit_kbps=config.bandwidth_limit_kbps,
                transfers=config.transfers,
                verify_after=config.verify_after_upload,
            )
        except (AgentUnavailable, AgentError) as exc:
            await self._record_failure(task_id, str(exc.detail))
            return
        except AppError as exc:
            await self._record_failure(task_id, str(exc.detail))
            return

        await self._attach_agent_task(task_id, accepted.task_id)
        self._current_agent_task = accepted.task_id

        agent_task = await self._poll(
            accepted.task_id, task_id=task_id, interval=await self._poll_interval()
        )
        if agent_task is None:
            return  # shutdown, or the agent stopped answering; recovery resolves it

        if agent_task.state == "success":
            await self._record_success(task_id, agent_task)
        else:
            await self._record_failure(
                task_id, agent_task.error or f"The transfer ended in state '{agent_task.state}'"
            )

    async def _load(self, task_id: int) -> tuple[str, str, str, str | None, int] | None:
        # Moving away from `uploaded` removes a retention safety proof (notably for a forced
        # re-upload), so it shares the same commit boundary as deletion classification.
        async with self._retention_guard.hold(), self._database.session() as session:
            task = await SqlAlchemySyncTaskRepository(session).get(task_id)
            if task is None:  # pragma: no cover - just claimed
                return None

            record = (
                await SqlAlchemyBackupHistoryRepository(session).get(task.backup_id)
                if task.backup_id is not None
                else None
            )
            if record is None or record.filename is None:
                task.status = SyncStatus.FAILED.value
                task.finished_at = datetime.now(UTC)
                task.error_message = "The backup this transfer refers to no longer exists"
                return None

            record.upload_status = UploadStatus.UPLOADING.value
            return (
                record.filename,
                task.remote_name or "",
                task.remote_path or "",
                task.correlation_id,
                task.attempt,
            )

    async def _poll(self, agent_task_id: str, *, task_id: int, interval: float) -> AgentTask | None:
        unreachable_since: float | None = None
        grace = self._settings.agent_unreachable_grace_seconds

        while not self._stopping.is_set():
            try:
                agent_task = await self._agent.get_task(agent_task_id)
            except (AgentUnavailable, AgentError) as exc:
                now = asyncio.get_running_loop().time()
                unreachable_since = unreachable_since or now
                waited = now - unreachable_since
                if waited > grace:
                    # Unlike a backup, retrying an upload is safe — the object is overwritten,
                    # not duplicated — so this goes back on the queue rather than to a state
                    # that needs a human.
                    await self._record_failure(
                        task_id,
                        f"The agent stopped answering for {int(waited)}s during the transfer "
                        f"({exc.detail})",
                    )
                    return None
                await self._sleep(min(interval * 2, 30))
                continue

            unreachable_since = None
            if agent_task.is_terminal:
                return agent_task

            await self._publish_progress(task_id, agent_task)
            await self._sleep(interval)

        return None

    async def _publish_progress(self, task_id: int, agent_task: AgentTask) -> None:
        progress = agent_task.progress
        async with self._database.session() as session:
            task = await SqlAlchemySyncTaskRepository(session).get(task_id)
            if task is None:  # pragma: no cover
                return
            # Bytes *are* persisted, unlike backup progress: a resumed transfer should not
            # report that it is starting from zero when it is not.
            task.bytes_transferred = progress.bytes_done or task.bytes_transferred
            task.bytes_total = progress.bytes_total or task.bytes_total
            task.transfer_rate_bps = progress.rate_bps

        self._events.publish(
            EVENT_SYNC_PROGRESS,
            sync_task_id=task_id,
            percent=progress.percent,
            bytes_done=progress.bytes_done,
            bytes_total=progress.bytes_total,
            rate_bps=progress.rate_bps,
            eta_seconds=progress.eta_seconds,
        )

    async def _attach_agent_task(self, task_id: int, agent_task_id: str) -> None:
        async with self._database.session() as session:
            task = await SqlAlchemySyncTaskRepository(session).get(task_id)
            if task is not None:
                task.agent_task_id = agent_task_id

    async def _record_success(self, task_id: int, agent_task: AgentTask) -> None:
        async with self._retention_guard.hold(), self._database.session() as session:
            tasks = SqlAlchemySyncTaskRepository(session)
            task = await tasks.get(task_id)
            if task is None:  # pragma: no cover
                return

            task.status = SyncStatus.SUCCESS.value
            task.finished_at = agent_task.finished_at or datetime.now(UTC)
            task.error_message = None
            result = agent_task.result
            if isinstance(result.get("bytes_transferred"), int):
                task.bytes_transferred = int(result["bytes_transferred"])

            record = (
                await SqlAlchemyBackupHistoryRepository(session).get(task.backup_id)
                if task.backup_id is not None
                else None
            )
            if record is not None:
                record.upload_status = UploadStatus.UPLOADED.value
                record.uploaded_at = task.finished_at
                record.remote_path = f"{task.remote_name}:{task.remote_path}"
                verification = result.get("verification")
                if isinstance(verification, dict):
                    record.remote_checksum = verification.get("remote_md5")
                    record.remote_size_bytes = verification.get("remote_size_bytes")
            backup_id = task.backup_id

        logger.info("sync_transfer_succeeded", sync_task_id=task_id, backup_id=backup_id)
        self._events.publish(
            EVENT_SYNC_STATE,
            sync_task_id=task_id,
            backup_id=backup_id,
            status=SyncStatus.SUCCESS.value,
        )
        if backup_id is not None and self._retention is not None:
            # The upload marker is durable now; retention never acts on an uncommitted claim
            # that a remote copy is safe.
            self._retention.notify(backup_id)

    async def _record_failure(self, task_id: int, message: str) -> None:
        """Schedule a retry, or give up and mark the backup's upload failed."""
        async with self._retention_guard.hold(), self._database.session() as session:
            tasks = SqlAlchemySyncTaskRepository(session)
            task = await tasks.get(task_id)
            if task is None:  # pragma: no cover
                return

            task.error_message = message[:2000]
            exhausted = task.attempt >= task.max_attempts

            if exhausted:
                task.status = SyncStatus.FAILED.value
                task.finished_at = datetime.now(UTC)
                task.next_retry_at = None
            else:
                delay = self._retry_delay(task.attempt)
                task.status = SyncStatus.QUEUED.value
                task.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)

            record = (
                await SqlAlchemyBackupHistoryRepository(session).get(task.backup_id)
                if task.backup_id is not None
                else None
            )
            if record is not None:
                # `pending` while a retry is scheduled, so the tile shows work outstanding
                # rather than a failure that is about to be attempted again.
                record.upload_status = (
                    UploadStatus.FAILED.value if exhausted else UploadStatus.PENDING.value
                )

            attempt, max_attempts = task.attempt, task.max_attempts
            retry_at, backup_id = task.next_retry_at, task.backup_id

            if exhausted:
                await self._notify_exhausted(
                    session, task_id=task_id, record=record, attempt=attempt, message=message
                )

        log = logger.error if retry_at is None else logger.warning
        log(
            "sync_transfer_failed",
            sync_task_id=task_id,
            backup_id=backup_id,
            attempt=attempt,
            max_attempts=max_attempts,
            retry_at=retry_at.isoformat() if retry_at else None,
            error=message[:200],
        )
        self._events.publish(
            EVENT_SYNC_STATE,
            sync_task_id=task_id,
            backup_id=backup_id,
            status=SyncStatus.FAILED.value if retry_at is None else SyncStatus.QUEUED.value,
            attempt=attempt,
            max_attempts=max_attempts,
            error=message[:200],
        )

    async def _notify_exhausted(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        record: BackupHistory | None,
        attempt: int,
        message: str,
    ) -> None:
        """Announce an upload only once its retries are spent.

        Alerting on each attempt would turn one Drive rate limit into a stream of warnings
        about a transfer that is still going to succeed.
        """
        if self._notifications is None:
            return
        await self._notifications.emit(
            session,
            NotificationEvent.UPLOAD_FAILED,
            dedupe_key=f"sync_task:{task_id}",
            unique=True,
            backup_id=record.id if record is not None else None,
            variables={
                "filename": record.filename if record is not None else None,
                "vmid": record.vmid if record is not None else None,
                "guest_type": record.guest_type if record is not None else None,
                "attempts": attempt,
                "error": message,
            },
        )

    def _retry_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Jittered because the common failure is a quota or rate limit shared by every pending
        transfer: retrying them in lockstep would reproduce the burst that caused it.
        """
        base = self._settings.sync_retry_base_seconds
        ceiling = min(self._settings.sync_retry_max_seconds, base * (2 ** max(attempt - 1, 0)))
        return random.uniform(base, ceiling) if ceiling > base else float(base)  # noqa: S311

    # ---- recovery ------------------------------------------------------------

    async def recover(self) -> None:
        """Re-queue transfers the previous process left running.

        Safe to re-run, unlike a backup: `rclone copyto` overwrites its destination, so at
        worst the bytes are sent twice. The attempt counter is *not* advanced — the process
        died, which is not the transfer's fault.
        """
        async with self._database.session() as session:
            tasks = SqlAlchemySyncTaskRepository(session)
            recovered = await tasks.running()
            for task in recovered:
                task.status = SyncStatus.QUEUED.value
                task.attempt = max(task.attempt - 1, 0)
                task.next_retry_at = None
                task.error_message = "Re-queued after the dashboard restarted mid-transfer"

        if recovered:
            logger.info("sync_transfers_requeued", count=len(recovered))
        self.notify()

    # ---- helpers -------------------------------------------------------------

    def _service(self, session: AsyncSession) -> SyncService:
        return SyncService(
            tasks=SqlAlchemySyncTaskRepository(session),
            history=SqlAlchemyBackupHistoryRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
            ),
            agent=self._agent,
        )

    async def _gdrive_config(self) -> GDriveSettings:
        async with self._database.session() as session:
            section = await self._settings_service(session).get_section(SettingsSection.GDRIVE)
        assert isinstance(section, GDriveSettings)
        return section

    async def _poll_interval(self) -> float:
        """A seam, as on the run executor: the settings schema floors this at one second,
        which is right in production and far too slow for a suite asserting sequences."""
        async with self._database.session() as session:
            section = await self._settings_service(session).get_section(SettingsSection.AGENT)
        assert isinstance(section, AgentSettings)
        return float(section.poll_interval_seconds)

    def _settings_service(self, session: AsyncSession) -> SettingsService:
        return SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
        )
