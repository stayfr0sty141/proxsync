"""Backup schedule endpoints.

Every write re-syncs the in-memory scheduler — but only **after** the transaction commits,
so a rolled-back change can never leave a trigger registered for a job that does not exist.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import (
    AuditRepositoryDep,
    ContainerDep,
    RequireAdmin,
    RequireOperator,
    RequireViewer,
    ScheduleServiceDep,
    SessionDep,
)
from app.api.mappers import job_to_response, run_to_response
from app.schemas.backup import (
    BackupJobCreate,
    BackupJobListResponse,
    BackupJobResponse,
    BackupJobUpdate,
    JobPreviewResponse,
    RunAcceptedResponse,
)
from app.schemas.enums import AuditAction

router = APIRouter(prefix="/backup-jobs", tags=["backup-jobs"])


@router.get("", summary="List backup schedules")
async def list_jobs(service: ScheduleServiceDep, user: RequireViewer) -> BackupJobListResponse:
    del user
    jobs = await service.list_jobs()
    return BackupJobListResponse(items=[job_to_response(job) for job in jobs], total=len(jobs))


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a backup schedule")
async def create_job(
    payload: BackupJobCreate,
    service: ScheduleServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
) -> BackupJobResponse:
    job = await service.create_job(payload, created_by=user.id)
    await audit.record(
        action=AuditAction.SETTINGS_CHANGED,
        user_id=user.id,
        username=user.username,
        resource_type="backup_job",
        resource_id=str(job.id),
        detail={"name": job.name, "cron": job.cron_expression, "enabled": job.enabled},
    )
    container.scheduler.sync_after_commit(session)
    return job_to_response(job)


@router.get("/{job_id}", summary="One backup schedule")
async def get_job(
    job_id: int, service: ScheduleServiceDep, user: RequireViewer
) -> BackupJobResponse:
    del user
    return job_to_response(await service.get_job(job_id))


@router.put("/{job_id}", summary="Replace a backup schedule")
async def update_job(
    job_id: int,
    payload: BackupJobUpdate,
    service: ScheduleServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
) -> BackupJobResponse:
    async with container.retention_guard.transaction(session):
        job = await service.update_job(job_id, payload)
        await audit.record(
            action=AuditAction.SETTINGS_CHANGED,
            user_id=user.id,
            username=user.username,
            resource_type="backup_job",
            resource_id=str(job_id),
            detail={"name": job.name, "cron": job.cron_expression, "enabled": job.enabled},
        )
        container.scheduler.sync_after_commit(session)
        # Only pre-M5 linked runs consult live job counts, but changing those counts must still
        # serialize with a pass and wake it. Explicit M5 run policies remain frozen.
        container.retention_worker.notify_after_commit(session, None)
    return job_to_response(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a schedule")
async def delete_job(
    job_id: int,
    service: ScheduleServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
) -> None:
    async with container.retention_guard.transaction(session):
        await service.delete_job(job_id)
        await audit.record(
            action=AuditAction.SETTINGS_CHANGED,
            user_id=user.id,
            username=user.username,
            resource_type="backup_job",
            resource_id=str(job_id),
            detail={"deleted": True},
        )
        container.scheduler.sync_after_commit(session)
        container.retention_worker.notify_after_commit(session, None)


@router.post("/{job_id}/enable", summary="Enable a schedule")
async def enable_job(
    job_id: int,
    service: ScheduleServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
) -> BackupJobResponse:
    del user
    job = await service.set_enabled(job_id, enabled=True)
    container.scheduler.sync_after_commit(session)
    return job_to_response(job)


@router.post("/{job_id}/disable", summary="Disable a schedule")
async def disable_job(
    job_id: int,
    service: ScheduleServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
) -> BackupJobResponse:
    del user
    job = await service.set_enabled(job_id, enabled=False)
    container.scheduler.sync_after_commit(session)
    return job_to_response(job)


@router.get("/{job_id}/preview", summary="Next fire times and resolved targets")
async def preview_job(
    job_id: int, service: ScheduleServiceDep, user: RequireViewer
) -> JobPreviewResponse:
    del user
    return await service.preview(job_id)


@router.post(
    "/{job_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a schedule now, out of band",
)
async def run_job(
    job_id: int,
    service: ScheduleServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> RunAcceptedResponse:
    job, run = await service.run_now(job_id, requested_by=user.id)
    await audit.record(
        action=AuditAction.BACKUP_STARTED,
        user_id=user.id,
        username=user.username,
        resource_type="backup_run",
        resource_id=str(run.id),
        detail={"job_id": job_id, "guests": run.guest_total},
    )
    container.runner.notify_after_commit(session)
    return RunAcceptedResponse(run=run_to_response(run, job_name=job.name))
