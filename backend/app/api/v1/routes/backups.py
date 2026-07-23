"""Manual backup and backup history endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AuditRepositoryDep,
    BackupHistoryRepositoryDep,
    BackupServiceDep,
    ContainerDep,
    RequireAdmin,
    RequireOperator,
    RequireViewer,
    SessionDep,
)
from app.api.mappers import backup_to_response, run_to_response
from app.core.errors import NotFound
from app.schemas.backup import (
    BackupDeleteResponse,
    BackupHistoryListResponse,
    BackupHistoryResponse,
    BackupLockRequest,
    BackupLogResponse,
    ManualBackupRequest,
    RunAcceptedResponse,
)
from app.schemas.enums import AuditAction, BackupStatus, GuestType, UploadStatus

router = APIRouter(tags=["backups"])

MAX_PAGE_SIZE = 200
DEFAULT_LOG_TAIL = 500


@router.post("/backups/run", status_code=status.HTTP_202_ACCEPTED, summary="Backup Now")
async def run_backup(
    payload: ManualBackupRequest,
    service: BackupServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> RunAcceptedResponse:
    """Queue a manual backup.

    202, not 200: the run is recorded and queued here and executed by the run worker. Holding
    the request open until vzdump finishes would tie a backup that can take an hour to an HTTP
    connection that will not survive it.
    """
    run = await service.request_manual_run(payload, requested_by=user.id)
    await audit.record(
        action=AuditAction.BACKUP_STARTED,
        user_id=user.id,
        username=user.username,
        resource_type="backup_run",
        resource_id=str(run.id),
        detail={
            "guests": [target.model_dump(mode="json") for target in payload.targets],
            "trigger": "manual",
        },
    )
    container.runner.notify_after_commit(session)
    return RunAcceptedResponse(run=run_to_response(run))


@router.get("/backups", summary="Backup history")
async def list_backups(
    history: BackupHistoryRepositoryDep,
    user: RequireViewer,
    vmid: int | None = None,
    guest_type: Annotated[GuestType | None, Query(alias="type")] = None,
    backup_status: Annotated[BackupStatus | None, Query(alias="status")] = None,
    upload_status: UploadStatus | None = None,
    run_id: int | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BackupHistoryListResponse:
    del user
    rows = await history.search(
        vmid=vmid,
        guest_type=guest_type,
        status=backup_status,
        upload_status=upload_status,
        run_id=run_id,
        since=date_from,
        until=date_to,
        query=search,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    total = await history.count(
        vmid=vmid,
        guest_type=guest_type,
        status=backup_status,
        upload_status=upload_status,
        run_id=run_id,
        since=date_from,
        until=date_to,
        query=search,
        include_deleted=include_deleted,
    )
    return BackupHistoryListResponse(
        items=[backup_to_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/backups/{backup_id}", summary="One backup")
async def get_backup(
    backup_id: int, service: BackupServiceDep, user: RequireViewer
) -> BackupHistoryResponse:
    del user
    return backup_to_response(await service.get_backup(backup_id))


@router.get("/backups/{backup_id}/log", summary="The agent's task log for a backup")
async def get_backup_log(
    backup_id: int,
    service: BackupServiceDep,
    container: ContainerDep,
    user: RequireViewer,
    tail: Annotated[int, Query(ge=1, le=10_000)] = DEFAULT_LOG_TAIL,
) -> BackupLogResponse:
    del user
    record = await service.get_backup(backup_id)
    if record.agent_task_id is None:
        raise NotFound(
            f"Backup #{backup_id} has no agent task, so there is no log to read — it failed "
            "before the agent was asked to start it. The reason is on the backup itself."
        )

    log = await container.agent.get_task_log(record.agent_task_id, tail=tail)
    return BackupLogResponse(
        backup_id=backup_id,
        task_id=log.task_id,
        lines=log.lines,
        truncated=log.truncated,
        total_lines=log.total_lines,
    )


@router.patch("/backups/{backup_id}/lock", summary="Pin a backup against retention")
async def set_lock(
    backup_id: int,
    payload: BackupLockRequest,
    service: BackupServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
) -> BackupHistoryResponse:
    async with container.retention_guard.transaction(session):
        record = await service.set_retention_lock(backup_id, locked=payload.retention_locked)
        await audit.record(
            action=AuditAction.RETENTION_OVERRIDE,
            user_id=user.id,
            username=user.username,
            resource_type="backup",
            resource_id=str(backup_id),
            detail={"retention_locked": payload.retention_locked, "filename": record.filename},
        )
        # Locking must stop a queued retry, while unlocking should not wait for another backup
        # or a process restart before the newly eligible artifact is evaluated.
        container.retention_worker.notify_after_commit(session, backup_id)
    return backup_to_response(record)


@router.delete("/backups/{backup_id}", summary="Delete a backup artifact")
async def delete_backup(
    backup_id: int,
    service: BackupServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
) -> BackupDeleteResponse:
    """Delete the local artifact.

    Refuses while the backup is running, and refuses a pinned one: an operator who set
    `retention_locked` asked for exactly this protection, and overriding it here would make
    the pin meaningless.

    This endpoint removes only the local copy. A Drive copy remains available until remote
    retention selects it; the history row stays `success` while either location is usable and
    becomes `deleted` only after both copies are gone.
    """
    async with container.retention_guard.transaction(session):
        filename, deleted = await service.delete_local_artifact(backup_id)
        await audit.record(
            action=AuditAction.BACKUP_DELETED,
            user_id=user.id,
            username=user.username,
            resource_type="backup",
            resource_id=str(backup_id),
            detail={"filename": filename, "freed_bytes": deleted.freed_bytes},
        )
        container.retention_worker.notify_after_commit(session, backup_id)
    return BackupDeleteResponse(
        backup_id=backup_id, deleted=deleted.deleted, freed_bytes=deleted.freed_bytes
    )
