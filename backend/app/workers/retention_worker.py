"""Event-driven, single-lane retention execution with bounded failure retries.

Startup synchronously re-derives one trigger per guest before backup workers can produce new
work. Afterwards only committed eligibility/settings notifications and delayed retries wake the
worker; there is no healthy-state polling that would append duplicate ``kept`` events. Every
queued id is canonicalised to that guest's newest existing copy immediately before execution,
so a stale completion cannot apply an older run's policy.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.crypto import SecretBox
from app.core.errors import NotFound, ValidationFailed
from app.core.logging import logger, set_correlation_id
from app.core.retention_guard import RetentionGuard
from app.db.session import Database
from app.repositories.retention_repository import SqlAlchemyRetentionRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.retention import RetentionResponse
from app.services.notification_service import NotificationGateway
from app.services.retention_service import RetentionAgent, RetentionService
from app.services.settings_service import SettingsService

DEFAULT_RETRY_DELAYS = (5.0, 30.0, 120.0)


class RetentionWorker:
    """Serialise passes, coalesce guest triggers, and retry transient failures."""

    def __init__(
        self,
        *,
        database: Database,
        agent: RetentionAgent,
        secret_box: SecretBox,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        guard: RetentionGuard | None = None,
        notifications: NotificationGateway | None = None,
    ) -> None:
        self._database = database
        self._agent = agent
        self._secret_box = secret_box
        self._guard = guard or RetentionGuard()
        self._notifications = notifications
        self._task: asyncio.Task[None] | None = None
        self._doorbell = asyncio.Event()
        self._stopping = asyncio.Event()
        self._execution_lock = asyncio.Lock()
        self._pending_backup_ids: set[int] = set()
        self._full_sweep_pending = False
        self._retry_delays = tuple(max(float(delay), 0.01) for delay in retry_delays)
        self._retry_attempts: dict[int | None, int] = {}
        self._retry_tasks: dict[int | None, asyncio.Task[None]] = {}
        self._errors: dict[int | None, str] = {}

    async def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        # This await is the startup crash-recovery guarantee. main.py starts the backup/sync
        # workers only after this returns, so no new completion can race ahead of recovery.
        await self.run_startup_sweep()
        self._task = asyncio.create_task(self._loop(), name="proxsync-retention-worker")
        logger.info("retention_worker_started")

    async def stop(self) -> None:
        self._stopping.set()
        self._doorbell.set()
        retry_tasks = list(self._retry_tasks.values())
        self._retry_tasks.clear()
        for retry_task in retry_tasks:
            retry_task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._task, timeout=10)
            self._task = None
        self._retry_attempts.clear()
        logger.info("retention_worker_stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> str | None:
        return list(self._errors.values())[-1] if self._errors else None

    def notify(self, backup_id: int | None = None) -> None:
        """Queue a guest-scoped pass, or a full pass when no backup id is supplied."""

        if backup_id is None:
            self._cancel_all_retries()
            self._full_sweep_pending = True
            self._pending_backup_ids.clear()
        else:
            self._cancel_retry(backup_id)
            if not self._full_sweep_pending:
                self._pending_backup_ids.add(backup_id)
        self._doorbell.set()

    def notify_after_commit(self, session: AsyncSession, backup_id: int | None) -> None:
        """Queue a pass only after the transaction exposing the changed row commits."""

        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _queue(_session: Session) -> None:
            self.notify(backup_id)

    async def run_once(self, *, backup_id: int | None = None) -> RetentionResponse:
        """Run one canonical pass; direct callers share the same deletion lane."""

        if backup_id is None:
            raise ValidationFailed(
                "A global retention apply is unsafe; use run_startup_sweep() "
                "so each guest resolves its own policy."
            )
        async with self._execution_lock, self._guard.hold():
            canonical = await self._canonical_trigger(backup_id)
            if canonical is None:
                self._clear_retry(backup_id)
                raise NotFound(f"No existing backup copy for trigger #{backup_id}")
            self._migrate_retry_state(backup_id, canonical)
            self._cancel_retry_task_only(canonical)
            try:
                response = await self._apply_one(canonical)
            except Exception as exc:
                self._set_error(
                    canonical,
                    f"Retention pass for backup #{canonical} failed: {exc}",
                )
                self._schedule_retry(canonical)
                raise
            self._finish_attempt(canonical, response)
            return response

    async def run_startup_sweep(self) -> list[RetentionResponse]:
        """Recover every guest independently; one broken pass cannot starve the rest."""

        async with self._execution_lock:
            try:
                async with self._database.session() as session:
                    repository = SqlAlchemyRetentionRepository(session)
                    trigger_ids = await repository.newest_trigger_backup_ids()
                    error_map = {
                        backup_id: await repository.canonical_trigger_backup_id(backup_id)
                        for backup_id in list(self._errors)
                        if backup_id is not None
                    }
            except Exception as exc:  # noqa: BLE001 - bounded retry handles transient scan failure
                self._set_error(None, f"Retention startup scan failed: {exc}")
                logger.error("retention_startup_scan_failed", exc_info=True)
                self._schedule_retry(None)
                return []
            self._clear_retry(None)
            for raw_id, canonical_id in error_map.items():
                if canonical_id is None:
                    self._clear_retry(raw_id)
                else:
                    self._migrate_retry_state(raw_id, canonical_id)

            responses: list[RetentionResponse] = []
            for raw_id in trigger_ids:
                async with self._guard.hold():
                    try:
                        backup_id = await self._canonical_trigger(raw_id)
                    except Exception as exc:  # noqa: BLE001 - continue with the next guest
                        self._set_error(
                            raw_id,
                            f"Retention trigger canonicalization failed: {exc}",
                        )
                        self._schedule_retry(raw_id)
                        continue
                    if backup_id is None:
                        self._clear_retry(raw_id)
                        continue
                    self._migrate_retry_state(raw_id, backup_id)
                    try:
                        response = await self._apply_one(backup_id)
                    except Exception as exc:  # noqa: BLE001 - continue with the next guest
                        self._set_error(
                            backup_id,
                            f"Retention pass for backup #{backup_id} failed: {exc}",
                        )
                        logger.error(
                            "retention_startup_guest_failed",
                            backup_id=backup_id,
                            exc_info=True,
                        )
                        self._schedule_retry(backup_id)
                        continue
                    responses.append(response)
                    self._finish_attempt(backup_id, response)
            return responses

    async def _apply_one(self, backup_id: int | None) -> RetentionResponse:
        set_correlation_id(None)
        async with self._database.session() as session:
            service = RetentionService(
                repository=SqlAlchemyRetentionRepository(session),
                settings_service=SettingsService(
                    repository=SqlAlchemySettingsRepository(session),
                    secret_box=self._secret_box,
                ),
                agent=self._agent,
                notifications=(
                    self._notifications.factory(session)
                    if self._notifications is not None
                    else None
                ),
            )
            response = await service.apply(backup_id=backup_id)
            if self._notifications is not None and self._notifications.doorbell is not None:
                # Rung after commit, like every other doorbell: the outbox row a pass may have
                # written is not visible to the delivery worker until this session closes.
                self._notifications.doorbell.notify_after_commit(session)
            return response

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            # Clear before draining so a notification arriving during the drain or execution
            # remains set for the next iteration.
            self._doorbell.clear()
            work = self._drain_pending()
            if not work:
                await self._wait_for_work()
                continue

            if work == [None]:
                await self.run_startup_sweep()
                continue
            await self._run_queued([backup_id for backup_id in work if backup_id is not None])

    async def _run_queued(self, backup_ids: list[int]) -> None:
        async with self._execution_lock:
            processed: set[int] = set()
            for raw_id in backup_ids:
                if self._stopping.is_set():
                    return
                async with self._guard.hold():
                    # Query immediately before this guest's apply and keep the shared guard
                    # through revalidation, agent I/O and marker commit.
                    try:
                        backup_id = await self._canonical_trigger(raw_id)
                    except Exception as exc:  # noqa: BLE001 - continue with other guests
                        self._set_error(
                            raw_id,
                            f"Retention trigger canonicalization failed: {exc}",
                        )
                        logger.error(
                            "retention_trigger_canonicalization_failed",
                            backup_id=raw_id,
                            exc_info=True,
                        )
                        self._schedule_retry(raw_id)
                        continue
                    if backup_id is None:
                        self._clear_retry(raw_id)
                        continue
                    if backup_id in processed:
                        self._clear_retry(raw_id)
                        continue
                    processed.add(backup_id)
                    self._migrate_retry_state(raw_id, backup_id)
                    self._cancel_retry_task_only(backup_id)
                    try:
                        response = await self._apply_one(backup_id)
                    except Exception as exc:  # noqa: BLE001 - one pass must not kill the worker
                        self._set_error(
                            backup_id,
                            f"Retention pass for backup #{backup_id} failed: {exc}",
                        )
                        logger.error(
                            "retention_worker_pass_failed",
                            backup_id=backup_id,
                            exc_info=True,
                        )
                        self._schedule_retry(backup_id)
                        continue
                    self._finish_attempt(backup_id, response)

    async def _canonical_trigger(self, backup_id: int) -> int | None:
        async with self._database.session() as session:
            return await SqlAlchemyRetentionRepository(session).canonical_trigger_backup_id(
                backup_id
            )

    def _drain_pending(self) -> list[int | None]:
        if self._full_sweep_pending:
            self._full_sweep_pending = False
            self._pending_backup_ids.clear()
            return [None]
        backup_ids = sorted(self._pending_backup_ids)
        self._pending_backup_ids.clear()
        return list(backup_ids)

    async def _wait_for_work(self) -> None:
        await self._doorbell.wait()

    def _finish_attempt(self, backup_id: int | None, response: RetentionResponse) -> None:
        if response.summary.failed:
            detail = next(
                (item.error for item in response.items if item.error),
                "one or more retention deletes failed",
            )
            self._set_error(backup_id, f"Retention pass failed: {detail}")
            self._schedule_retry(backup_id)
        else:
            self._clear_retry(backup_id)

    def _schedule_retry(self, backup_id: int | None) -> None:
        existing = self._retry_tasks.get(backup_id)
        if existing is not None and not existing.done():
            return
        attempt = self._retry_attempts.get(backup_id, 0)
        if attempt >= len(self._retry_delays):
            self._set_error(
                backup_id,
                f"Retention retries exhausted for "
                f"{'full sweep' if backup_id is None else f'backup #{backup_id}'}",
            )
            logger.error(
                "retention_retry_exhausted",
                backup_id=backup_id,
                attempts=attempt,
            )
            return
        delay = self._retry_delays[attempt]
        self._retry_attempts[backup_id] = attempt + 1
        task = asyncio.create_task(
            self._retry_after(backup_id, delay),
            name=f"proxsync-retention-retry-{backup_id or 'full'}-{attempt + 1}",
        )
        self._retry_tasks[backup_id] = task
        logger.warning(
            "retention_retry_scheduled",
            backup_id=backup_id,
            attempt=attempt + 1,
            delay_seconds=delay,
        )

    async def _retry_after(self, backup_id: int | None, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            current = asyncio.current_task()
            if self._retry_tasks.get(backup_id) is current:
                self._retry_tasks.pop(backup_id, None)

        if self._stopping.is_set():
            return
        if backup_id is None:
            self._full_sweep_pending = True
            self._pending_backup_ids.clear()
        elif not self._full_sweep_pending:
            self._pending_backup_ids.add(backup_id)
        self._doorbell.set()

    def _clear_retry(self, backup_id: int | None) -> None:
        self._retry_attempts.pop(backup_id, None)
        self._errors.pop(backup_id, None)
        task = self._retry_tasks.pop(backup_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _cancel_retry(self, backup_id: int | None) -> None:
        self._retry_attempts.pop(backup_id, None)
        self._cancel_retry_task_only(backup_id)

    def _cancel_all_retries(self) -> None:
        tasks = list(self._retry_tasks.values())
        self._retry_tasks.clear()
        self._retry_attempts.clear()
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()

    def _cancel_retry_task_only(self, backup_id: int | None) -> None:
        task = self._retry_tasks.pop(backup_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _migrate_retry_state(self, raw_id: int, canonical_id: int) -> None:
        if raw_id == canonical_id:
            return
        raw_attempt = self._retry_attempts.pop(raw_id, None)
        if raw_attempt is not None:
            self._retry_attempts[canonical_id] = max(
                raw_attempt,
                self._retry_attempts.get(canonical_id, 0),
            )
        raw_error = self._errors.pop(raw_id, None)
        if raw_error is not None:
            self._errors[canonical_id] = raw_error
        self._cancel_retry_task_only(raw_id)

    def _set_error(self, backup_id: int | None, message: str) -> None:
        self._errors[backup_id] = message
