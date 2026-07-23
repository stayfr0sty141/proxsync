"""Schedule firing.

**APScheduler holds no persistent job store, on purpose.** The obvious wiring — a
`SQLAlchemyJobStore` pointed at the same database — would give ProxSync two sources of truth
for the same fact: `backup_jobs` and APScheduler's `apscheduler_jobs`. They drift the first
time a row is edited outside the app, restored from a backup, or migrated, and the failure is
silent: the UI shows a schedule that never fires, or a schedule fires that the UI does not
show. Instead `backup_jobs` is authoritative and the in-memory scheduler is rebuilt from it.

That trade costs one thing — a missed firing is not replayed automatically by APScheduler —
so `next_run_at` is persisted and reconciled at startup instead. A schedule missed inside the
grace window runs immediately; one missed beyond it is skipped and logged loudly, because a
weekly backup starting unattended three days late is more surprising than one that did not
start at all.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.cron import build_trigger, next_fire_time
from app.core.crypto import SecretBox
from app.core.errors import AppError
from app.core.events import EVENT_JOB_STATE, EventBus
from app.core.logging import logger, set_correlation_id
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.backup_job_repository import SqlAlchemyBackupJobRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import SettingsSection, TriggerType
from app.schemas.settings import ProxmoxSettings
from app.services.backup_service import BackupService
from app.services.inventory_service import InventoryService
from app.services.settings_service import SettingsService
from app.workers.backup_runner import BackupRunner

JOB_PREFIX = "backup-job-"
INVENTORY_JOB_ID = "inventory-refresh"
INVENTORY_INITIAL_DELAY = 5.0
"""Seconds after startup before the first inventory refresh."""

InventoryFactory = Callable[[AsyncSession], InventoryService]
"""Built per session by the composition root, so the scheduler needs no Proxmox knowledge."""


def job_key(job_id: int) -> str:
    return f"{JOB_PREFIX}{job_id}"


class SchedulerWorker:
    def __init__(
        self,
        *,
        database: Database,
        runner: BackupRunner,
        events: EventBus,
        settings: Settings,
        secret_box: SecretBox,
        inventory_factory: InventoryFactory,
    ) -> None:
        self._database = database
        self._runner = runner
        self._events = events
        self._settings = settings
        self._secret_box = secret_box
        self._inventory_factory = inventory_factory
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._scheduler.start()

        # Read the missed schedules *first*. `sync_jobs` recomputes `next_run_at` from the
        # cron expression, which overwrites the very timestamp that says a firing was missed
        # — catch-up would then never trigger, and the misfire policy would be decorative.
        missed = await self._collect_missed_schedules()

        await self.sync_jobs()
        await self._schedule_inventory_refresh()
        await self._fire_missed_schedules(missed)

        logger.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def job_count(self) -> int:
        return len(self._scheduler.get_jobs())

    # ---- registration --------------------------------------------------------

    def sync_after_commit(self, session: AsyncSession) -> None:
        """Rebuild the schedule once the transaction that changed a job commits.

        Rebuilding inside the request would register a trigger for a row that a later error
        rolls back, leaving the scheduler firing a job that does not exist.
        """

        @event.listens_for(session.sync_session, "after_commit", once=True)
        def _resync(_session: Session) -> None:
            self._scheduler.add_job(
                self.sync_jobs,
                trigger="date",
                run_date=datetime.now(UTC),
                id="scheduler-resync",
                replace_existing=True,
                misfire_grace_time=None,
            )

    async def sync_jobs(self) -> None:
        """Make the in-memory schedule match the `backup_jobs` table exactly."""
        async with self._database.session() as session:
            jobs = await SqlAlchemyBackupJobRepository(session).list_enabled()
            wanted = {job.id: (job.cron_expression, job.timezone, job.name) for job in jobs}

            for job in jobs:
                try:
                    job.next_run_at = next_fire_time(job.cron_expression, job.timezone)
                except AppError:
                    # Reported below, where the trigger is built. Leaving the stale value in
                    # place is correct: nothing is scheduled, so nothing new is due.
                    continue

        for existing in self._scheduler.get_jobs():
            if existing.id.startswith(JOB_PREFIX):
                job_id = int(existing.id.removeprefix(JOB_PREFIX))
                if job_id not in wanted:
                    self._scheduler.remove_job(existing.id)

        for job_id, (expression, timezone, name) in wanted.items():
            try:
                trigger = build_trigger(expression, timezone)
            except AppError:
                # Stored expressions are validated on write, so this means the row was edited
                # outside the API. Skip that one job rather than refusing to schedule any.
                logger.error("scheduler_invalid_cron", job_id=job_id, cron=expression, name=name)
                continue

            self._scheduler.add_job(
                self._fire,
                trigger=trigger,
                args=[job_id],
                id=job_key(job_id),
                name=name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=self._settings.scheduler_misfire_grace_seconds,
            )

        logger.info("scheduler_synced", jobs=len(wanted))

    async def _schedule_inventory_refresh(self) -> None:
        async with self._database.session() as session:
            service = SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=self._secret_box,
            )
            section = await service.get_section(SettingsSection.PROXMOX)
        assert isinstance(section, ProxmoxSettings)

        self._scheduler.add_job(
            self._refresh_inventory,
            trigger="interval",
            seconds=section.inventory_refresh_seconds,
            id=INVENTORY_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            # Shortly after start, not during it: an unreachable PVE would otherwise hold up
            # startup for the client timeout, and the dashboard must come up regardless.
            next_run_time=datetime.now(UTC) + timedelta(seconds=INVENTORY_INITIAL_DELAY),
        )

    # ---- firing --------------------------------------------------------------

    async def _fire(self, job_id: int) -> None:
        """Turn a schedule firing into a queued run. Never talks to the agent itself."""
        set_correlation_id(None)
        async with self._database.session() as session:
            jobs = SqlAlchemyBackupJobRepository(session)
            job = await jobs.get(job_id)
            if job is None or not job.enabled:
                logger.warning("scheduler_fired_missing_job", job_id=job_id)
                return

            service = self._backup_service(session)
            try:
                run = await service.request_job_run(
                    job, trigger=TriggerType.SCHEDULE, requested_by=None
                )
            except AppError as exc:
                # A schedule that resolves to nothing, or overlaps its previous run, is an
                # operational condition and not a crash. It must be visible, not swallowed.
                logger.error(
                    "scheduled_run_not_started",
                    job_id=job_id,
                    name=job.name,
                    reason=exc.detail,
                )
                self._events.publish(
                    EVENT_JOB_STATE, job_id=job_id, status="not_started", detail=exc.detail
                )
                return

            job.last_run_at = datetime.now(UTC)
            job.next_run_at = next_fire_time(job.cron_expression, job.timezone)
            run_id = run.id

        logger.info("scheduled_run_queued", job_id=job_id, run_id=run_id)
        self._events.publish(EVENT_JOB_STATE, job_id=job_id, status="queued", run_id=run_id)
        self._runner.notify()

    async def _collect_missed_schedules(self) -> list[tuple[int, str, float]]:
        """Schedules whose time passed while the dashboard was down, with how late each is.

        Must run before `sync_jobs`, which recomputes `next_run_at` and would erase the
        evidence.
        """
        now = datetime.now(UTC)
        async with self._database.session() as session:
            jobs = await SqlAlchemyBackupJobRepository(session).list_enabled()
            return [
                (job.id, job.name, (now - job.next_run_at).total_seconds())
                for job in jobs
                if job.next_run_at is not None and job.next_run_at < now
            ]

    async def _fire_missed_schedules(self, missed: list[tuple[int, str, float]]) -> None:
        grace = self._settings.scheduler_misfire_grace_seconds

        for job_id, name, late_by in missed:
            if late_by > grace:
                # `sync_jobs` has already moved `next_run_at` forward, so the schedule
                # resumes normally; only this one firing is forgone.
                logger.warning(
                    "scheduled_run_skipped_too_late",
                    job_id=job_id,
                    name=name,
                    late_by_seconds=round(late_by),
                    grace_seconds=grace,
                )
                continue

            logger.info(
                "scheduled_run_catching_up",
                job_id=job_id,
                name=name,
                late_by_seconds=round(late_by),
            )
            await self._fire(job_id)

    async def _refresh_inventory(self) -> None:
        async with self._database.session() as session:
            service = self._inventory_factory(session)
            try:
                await service.sync()
            except AppError as exc:
                # PVE being briefly unreachable is normal during a host reboot. The cached
                # inventory keeps serving; the next tick tries again.
                logger.warning("inventory_refresh_failed", reason=exc.detail)

    def _backup_service(self, session: AsyncSession) -> BackupService:
        return BackupService(
            guests=SqlAlchemyGuestRepository(session),
            runs=SqlAlchemyBackupRunRepository(session),
            history=SqlAlchemyBackupHistoryRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=self._secret_box,
            ),
        )
