"""Backup schedule use cases.

The `backup_jobs` table is the single source of truth for what is scheduled. APScheduler
holds a *derived* view in memory, rebuilt from this table on start and after every change —
see `app.workers.scheduler` for why the scheduler does not own a persistent job store.

Cron expressions are validated here, on the request that introduces them, so a typo is a 400
rather than a job that silently never fires.
"""

from __future__ import annotations

from app.core.cron import next_fire_time, next_fire_times, validate_cron
from app.core.errors import Conflict, NotFound
from app.core.logging import logger
from app.db.models.backup import BackupJob, BackupJobTarget, BackupRun
from app.repositories.backup_job_repository import SqlAlchemyBackupJobRepository
from app.schemas.backup import (
    BackupJobCreate,
    BackupJobUpdate,
    GuestTarget,
    JobPreviewResponse,
)
from app.schemas.enums import GuestType, TriggerType
from app.services.backup_service import BackupService

PREVIEW_FIRE_TIMES = 5


class ScheduleService:
    def __init__(
        self,
        *,
        jobs: SqlAlchemyBackupJobRepository,
        backups: BackupService,
    ) -> None:
        self._jobs = jobs
        self._backups = backups

    # ---- reads ---------------------------------------------------------------

    async def list_jobs(self) -> list[BackupJob]:
        return await self._jobs.list_all()

    async def get_job(self, job_id: int) -> BackupJob:
        job = await self._jobs.get(job_id)
        if job is None:
            raise NotFound(f"No backup job with id {job_id}")
        return job

    async def preview(self, job_id: int) -> JobPreviewResponse:
        job = await self.get_job(job_id)
        resolution = await self._backups.resolve_job_targets(job)
        return JobPreviewResponse(
            job_id=job.id,
            cron_expression=job.cron_expression,
            timezone=job.timezone,
            next_fire_times=next_fire_times(
                job.cron_expression, job.timezone, count=PREVIEW_FIRE_TIMES
            ),
            resolved_targets=resolution.targets,
            skipped=resolution.skipped,
        )

    # ---- writes --------------------------------------------------------------

    async def create_job(self, payload: BackupJobCreate, *, created_by: int | None) -> BackupJob:
        if await self._jobs.get_by_name(payload.name) is not None:
            raise Conflict(f"A backup job named '{payload.name}' already exists")

        expression = validate_cron(payload.cron_expression, payload.timezone)
        job = BackupJob(
            name=payload.name,
            enabled=payload.enabled,
            cron_expression=expression,
            timezone=payload.timezone,
            mode=payload.mode.value,
            compression=payload.compression.value,
            zstd_threads=payload.zstd_threads,
            storage=payload.storage,
            target_selector=payload.target_selector.value,
            keep_local=payload.keep_local,
            keep_remote=payload.keep_remote,
            upload_enabled=payload.upload_enabled,
            notify_on_start=payload.notify_on_start,
            notify_on_success=payload.notify_on_success,
            notify_on_failure=payload.notify_on_failure,
            bwlimit_kbps=payload.bwlimit_kbps,
            max_parallel=payload.max_parallel,
            created_by=created_by,
            next_run_at=next_fire_time(expression, payload.timezone) if payload.enabled else None,
            # Assigned in the constructor, not after the flush. `BackupJob.targets` is
            # `lazy="raise"`, so touching it on an already-persisted instance that never
            # loaded the collection raises rather than emitting a surprise query.
            targets=[
                BackupJobTarget(vmid=target.vmid, guest_type=target.guest_type.value)
                for target in payload.targets
            ],
        )
        job = await self._jobs.add(job)

        logger.info(
            "backup_job_created",
            job_id=job.id,
            name=job.name,
            cron=job.cron_expression,
            timezone=job.timezone,
            enabled=job.enabled,
        )
        return job

    async def update_job(self, job_id: int, payload: BackupJobUpdate) -> BackupJob:
        job = await self.get_job(job_id)

        clash = await self._jobs.get_by_name(payload.name)
        if clash is not None and clash.id != job.id:
            raise Conflict(f"A backup job named '{payload.name}' already exists")

        expression = validate_cron(payload.cron_expression, payload.timezone)
        job.name = payload.name
        job.enabled = payload.enabled
        job.cron_expression = expression
        job.timezone = payload.timezone
        job.mode = payload.mode.value
        job.compression = payload.compression.value
        job.zstd_threads = payload.zstd_threads
        job.storage = payload.storage
        job.target_selector = payload.target_selector.value
        job.keep_local = payload.keep_local
        job.keep_remote = payload.keep_remote
        job.upload_enabled = payload.upload_enabled
        job.notify_on_start = payload.notify_on_start
        job.notify_on_success = payload.notify_on_success
        job.notify_on_failure = payload.notify_on_failure
        job.bwlimit_kbps = payload.bwlimit_kbps
        job.max_parallel = payload.max_parallel
        job.next_run_at = next_fire_time(expression, payload.timezone) if payload.enabled else None

        await self._jobs.replace_targets(job, _target_pairs(payload.targets))
        logger.info("backup_job_updated", job_id=job.id, name=job.name, enabled=job.enabled)
        return job

    async def set_enabled(self, job_id: int, *, enabled: bool) -> BackupJob:
        job = await self.get_job(job_id)
        job.enabled = enabled
        job.next_run_at = next_fire_time(job.cron_expression, job.timezone) if enabled else None
        await self._jobs.flush()
        logger.info("backup_job_enabled_changed", job_id=job.id, enabled=enabled)
        return job

    async def run_now(
        self, job_id: int, *, requested_by: int | None
    ) -> tuple[BackupJob, BackupRun]:
        """Queue a job's run out of band. The trigger is recorded as `manual`, not `schedule`,
        so history distinguishes "the weekly backup ran" from "someone pressed the button"."""
        job = await self.get_job(job_id)
        run = await self._backups.request_job_run(
            job, trigger=TriggerType.MANUAL, requested_by=requested_by
        )
        return job, run

    async def delete_job(self, job_id: int) -> None:
        job = await self.get_job(job_id)
        await self._jobs.delete(job)
        logger.info("backup_job_deleted", job_id=job_id)


def _target_pairs(targets: list[GuestTarget]) -> list[tuple[int, GuestType]]:
    return [(target.vmid, target.guest_type) for target in targets]
