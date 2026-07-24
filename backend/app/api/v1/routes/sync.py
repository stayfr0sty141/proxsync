"""Google Drive sync and backup-browser endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from app.api.deps import (
    AuditRepositoryDep,
    ContainerDep,
    RequireOperator,
    RequireViewer,
    SessionDep,
    SyncServiceDep,
)
from app.core.errors import AgentError, AgentUnavailable
from app.schemas.enums import AuditAction, SyncStatus
from app.schemas.sync import (
    BrowserListResponse,
    ComparisonResponse,
    RemoteQuotaResponse,
    SyncQueueStatus,
    SyncTaskListResponse,
    SyncTaskResponse,
    UploadRequest,
    VerifyResponse,
)

router = APIRouter(tags=["sync"])

MAX_PAGE_SIZE = 200


# ---- transfers ---------------------------------------------------------------


@router.post(
    "/backups/{backup_id}/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a backup to Google Drive",
)
@router.post(
    "/sync/backups/{backup_id}/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a backup to Google Drive",
)
async def upload_backup(
    backup_id: int,
    payload: UploadRequest,
    service: SyncServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> SyncTaskResponse:
    async with container.retention_guard.transaction(session):
        task = await service.enqueue_upload(backup_id, force=payload.force)
        await audit.record(
            action=AuditAction.SETTINGS_CHANGED,
            user_id=user.id,
            username=user.username,
            resource_type="sync_task",
            resource_id=str(task.id),
            detail={"backup_id": backup_id, "force": payload.force},
        )
        container.sync_worker.notify_after_commit(session)
        container.retention_worker.notify_after_commit(session, backup_id)
        return await service.to_response(task)


class UploadArtifactPayload(BaseModel):
    filename: str
    force: bool = True


@router.post(
    "/upload_artifact",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an artifact by filename to Google Drive",
)
@router.post(
    "/sync/upload_artifact",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an artifact by filename to Google Drive",
)
async def upload_artifact(
    payload: UploadArtifactPayload,
    service: SyncServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> SyncTaskResponse:
    async with container.retention_guard.transaction(session):
        task = await service.enqueue_upload_by_filename(payload.filename, force=payload.force)
        await audit.record(
            action=AuditAction.SETTINGS_CHANGED,
            user_id=user.id,
            username=user.username,
            resource_type="sync_task",
            resource_id=str(task.id),
            detail={"filename": payload.filename, "force": payload.force},
        )
        container.sync_worker.notify_after_commit(session)
        if task.backup_id:
            container.retention_worker.notify_after_commit(session, task.backup_id)
        return await service.to_response(task)


@router.post("/backups/{backup_id}/verify", summary="Compare a backup with its remote copy")
@router.post("/sync/backups/{backup_id}/verify", summary="Compare a backup with its remote copy")
async def verify_backup(
    backup_id: int,
    service: SyncServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
) -> VerifyResponse:
    """Synchronous, and it *writes*: a copy found missing or corrupt clears the row's
    `uploaded` state, because retention must not delete a local artifact on the strength of a
    remote file that is not there."""
    del user
    async with container.retention_guard.transaction(session):
        result = await service.verify_backup(backup_id)
        # A successful verification may make deletion eligible; a mismatch clears the upload
        # proof and must invalidate any queued candidate. Both transitions are relevant.
        container.retention_worker.notify_after_commit(session, backup_id)
    return result


# ---- queue -------------------------------------------------------------------


@router.get("/sync/status", summary="Transfer queue status")
async def sync_status(
    service: SyncServiceDep, container: ContainerDep, user: RequireViewer
) -> SyncQueueStatus:
    del user
    current_id = container.sync_worker.current_task_id
    current = await service.get_task(current_id) if current_id is not None else None
    return await service.queue_status(current=current)


@router.get("/sync/tasks", summary="Recent transfers")
async def list_sync_tasks(
    container: ContainerDep,
    service: SyncServiceDep,
    session: SessionDep,
    user: RequireViewer,
    task_status: Annotated[SyncStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SyncTaskListResponse:
    del user, container
    from app.repositories.sync_task_repository import SqlAlchemySyncTaskRepository

    repository = SqlAlchemySyncTaskRepository(session)
    rows = await repository.list_recent(limit=limit, offset=offset, status=task_status)
    return SyncTaskListResponse(
        items=[await service.to_response(row) for row in rows],
        total=await repository.count(status=task_status),
    )


@router.get("/sync/quota", summary="Remote storage quota")
async def remote_quota(
    service: SyncServiceDep, container: ContainerDep, user: RequireViewer
) -> RemoteQuotaResponse:
    """Never raises for an unreachable remote: this feeds a storage tile, and a dashboard
    that 500s because Google is briefly unavailable is worse than one saying so."""
    del user
    config = await service.config()
    if not config.enabled:
        return RemoteQuotaResponse(
            remote=config.remote_name,
            configured=False,
            detail="Google Drive sync is disabled",
        )

    try:
        quota = await container.agent.remote_quota(remote=config.remote_name)
    except (AgentUnavailable, AgentError) as exc:
        return RemoteQuotaResponse(remote=config.remote_name, configured=True, detail=exc.detail)

    return RemoteQuotaResponse(
        remote=config.remote_name,
        configured=True,
        total_bytes=quota.total_bytes,
        used_bytes=quota.used_bytes,
        free_bytes=quota.free_bytes,
        trashed_bytes=quota.trashed_bytes,
        used_percent=quota.used_percent,
    )


# ---- browser -----------------------------------------------------------------


@router.get("/browser/local", summary="Artifacts on the local HDD")
async def browse_local(service: SyncServiceDep, user: RequireViewer) -> BrowserListResponse:
    del user
    entries = await service.local_entries()
    return BrowserListResponse(location="local", entries=entries, total=len(entries))


@router.get("/browser/remote", summary="Artifacts on Google Drive")
async def browse_remote(service: SyncServiceDep, user: RequireViewer) -> BrowserListResponse:
    del user
    entries = await service.remote_entries()
    return BrowserListResponse(location="remote", entries=entries, total=len(entries))


@router.get("/browser/compare", summary="Local versus remote")
async def compare(service: SyncServiceDep, user: RequireViewer) -> ComparisonResponse:
    del user
    return await service.compare()
