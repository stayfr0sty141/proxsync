"""Backup run endpoints — the run-level grouping over `backup_history`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import (
    AuditRepositoryDep,
    BackupHistoryRepositoryDep,
    BackupRunRepositoryDep,
    BackupServiceDep,
    ContainerDep,
    RequireOperator,
    RequireViewer,
)
from app.api.mappers import backup_to_response, run_to_response
from app.core.errors import Conflict
from app.schemas.backup import (
    BackupHistoryListResponse,
    BackupRunListResponse,
    BackupRunResponse,
)
from app.schemas.enums import AuditAction, RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])

MAX_PAGE_SIZE = 200


@router.get("", summary="List backup runs")
async def list_runs(
    runs: BackupRunRepositoryDep,
    container: ContainerDep,
    user: RequireViewer,
    status: RunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BackupRunListResponse:
    del user
    rows = await runs.list_recent(limit=limit, offset=offset, status=status)
    return BackupRunListResponse(
        items=[
            run_to_response(run, progress=container.runner.progress_for(run.id)) for run in rows
        ],
        total=await runs.count(status=status),
    )


@router.get("/{run_id}", summary="One backup run")
async def get_run(
    run_id: int,
    service: BackupServiceDep,
    container: ContainerDep,
    user: RequireViewer,
) -> BackupRunResponse:
    del user
    run = await service.get_run(run_id)
    return run_to_response(run, progress=container.runner.progress_for(run_id))


@router.get("/{run_id}/backups", summary="The artifacts produced by a run")
async def run_backups(
    run_id: int,
    service: BackupServiceDep,
    history: BackupHistoryRepositoryDep,
    user: RequireViewer,
) -> BackupHistoryListResponse:
    del user
    await service.get_run(run_id)
    rows = await history.for_run(run_id)
    return BackupHistoryListResponse(
        items=[backup_to_response(row) for row in rows],
        total=len(rows),
        limit=len(rows),
        offset=0,
    )


@router.post("/{run_id}/cancel", summary="Cancel a run")
async def cancel_run(
    run_id: int,
    service: BackupServiceDep,
    container: ContainerDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> BackupRunResponse:
    run = await service.get_run(run_id)

    if run.status == RunStatus.QUEUED.value:
        run = await service.cancel_queued_run(run_id)
    elif run.status == RunStatus.RUNNING.value:
        # The executor owns the agent task id, so it performs the cancellation; the run
        # reaches its terminal state when the current backup reports back.
        await container.runner.cancel(run_id)
    else:
        raise Conflict(f"Run #{run_id} is already '{run.status}' and cannot be cancelled.")

    await audit.record(
        action=AuditAction.BACKUP_STARTED,
        user_id=user.id,
        username=user.username,
        resource_type="backup_run",
        resource_id=str(run_id),
        detail={"cancelled": True},
    )
    return run_to_response(run, progress=container.runner.progress_for(run_id))
