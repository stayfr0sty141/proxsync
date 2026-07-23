"""Restore endpoints — the two-phase flow, and the history of what it did.

Every mutation here runs inside the shared retention guard. That is not ceremony: retention's
blocker is "an active restore references this backup", and a row that is not yet committed is
not a row retention can see. Creating the request under the guard is what makes the archive
safe from the moment preflight approved it, rather than from some point shortly afterwards.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AuditRepositoryDep,
    ContainerDep,
    RequireOperator,
    RequireViewer,
    RestoreServiceDep,
    SessionDep,
)
from app.api.mappers import restore_to_response
from app.core.errors import Conflict, NotFound
from app.schemas.enums import AuditAction, RestoreStatus
from app.schemas.restore import (
    PreflightReport,
    RestoreConfirmRequest,
    RestoreCreatedResponse,
    RestoreListResponse,
    RestoreLogResponse,
    RestoreRequest,
    RestoreResponse,
)

router = APIRouter(prefix="/restores", tags=["restores"])

MAX_PAGE_SIZE = 200
DEFAULT_LOG_TAIL = 500


@router.post("/preflight", summary="Check a restore without creating one")
async def preflight(
    payload: RestoreRequest, service: RestoreServiceDep, user: RequireOperator
) -> PreflightReport:
    """Read-only. Nothing is recorded, nothing is reserved, and a blocking report is a 200 —
    "this would not work, here is why" is the answer, not an error."""
    del user
    return await service.preflight(payload)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Request a restore")
async def create_restore(
    payload: RestoreRequest,
    service: RestoreServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> RestoreCreatedResponse:
    """Record the intent and return a confirmation token.

    201, not 202: nothing has been queued. The restore exists as a *request* that expires on
    its own if nobody confirms it.
    """
    async with container.retention_guard.transaction(session):
        record, token = await service.create(payload, requested_by=user.id)
        await audit.record(
            action=AuditAction.RESTORE_REQUESTED,
            user_id=user.id,
            username=user.username,
            resource_type="restore",
            resource_id=str(record.id),
            detail={
                "backup_id": payload.backup_id,
                "target_vmid": record.target_vmid,
                "overwrite_existing": record.overwrite_existing,
                "source": record.source,
            },
        )
        backup = await service.source_backup(record)
        base = restore_to_response(record, source_backup=backup)

    assert record.confirmation_expires_at is not None  # set by the service
    return RestoreCreatedResponse(
        **base.model_dump(),
        confirmation_token=token,
        expires_at=record.confirmation_expires_at,
    )


@router.post(
    "/{restore_id}/confirm",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Confirm and queue a restore",
)
async def confirm_restore(
    restore_id: int,
    payload: RestoreConfirmRequest,
    service: RestoreServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> RestoreResponse:
    """202: the restore is authorised and queued. The executor runs it, because `qmrestore`
    takes far longer than an HTTP request may be held open for."""
    async with container.retention_guard.transaction(session):
        record = await service.confirm(restore_id, payload)
        await audit.record(
            action=AuditAction.RESTORE_CONFIRMED,
            user_id=user.id,
            username=user.username,
            resource_type="restore",
            resource_id=str(restore_id),
            detail={
                "backup_id": record.backup_id,
                "target_vmid": record.target_vmid,
                "force_stop": record.force_stop,
            },
        )
        container.restore_worker.notify_after_commit(session)
        backup = await service.source_backup(record)
        return restore_to_response(record, source_backup=backup)


@router.post("/{restore_id}/cancel", summary="Cancel a restore")
async def cancel_restore(
    restore_id: int,
    service: RestoreServiceDep,
    container: ContainerDep,
    session: SessionDep,
    user: RequireOperator,
    audit: AuditRepositoryDep,
) -> RestoreResponse:
    """Stop a restore before it reaches the host, or ask the agent to stop one in flight.

    A running restore does not become `cancelled` here. It reaches its terminal state when the
    agent reports back, because only the agent knows whether the process died before or after
    it had begun rewriting the guest.
    """
    async with container.retention_guard.transaction(session):
        record = await service.get(restore_id)

        if record.status == RestoreStatus.RUNNING.value:
            if not await container.restore_worker.cancel(restore_id):
                raise Conflict(
                    f"Restore #{restore_id} is running but could not be cancelled at the "
                    "agent. Check the Backup Agent, then the guest on the node."
                )
        else:
            record = await service.cancel(restore_id)

        await audit.record(
            action=AuditAction.RESTORE_CANCELLED,
            user_id=user.id,
            username=user.username,
            resource_type="restore",
            resource_id=str(restore_id),
            detail={"previous_status": record.status, "target_vmid": record.target_vmid},
        )
        backup = await service.source_backup(record)
        return restore_to_response(record, source_backup=backup)


@router.get("", summary="Restore history")
async def list_restores(
    service: RestoreServiceDep,
    user: RequireViewer,
    restore_status: Annotated[RestoreStatus | None, Query(alias="status")] = None,
    backup_id: int | None = None,
    target_vmid: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RestoreListResponse:
    del user
    rows, total = await service.search(
        status=restore_status,
        backup_id=backup_id,
        target_vmid=target_vmid,
        limit=limit,
        offset=offset,
    )
    return RestoreListResponse(
        items=[
            restore_to_response(record, source_backup=await service.source_backup(record))
            for record in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{restore_id}", summary="One restore")
async def get_restore(
    restore_id: int, service: RestoreServiceDep, user: RequireViewer
) -> RestoreResponse:
    del user
    record = await service.get(restore_id)
    return restore_to_response(record, source_backup=await service.source_backup(record))


@router.get("/{restore_id}/log", summary="The agent's task log for a restore")
async def get_restore_log(
    restore_id: int,
    service: RestoreServiceDep,
    container: ContainerDep,
    user: RequireViewer,
    tail: Annotated[int, Query(ge=1, le=10_000)] = DEFAULT_LOG_TAIL,
) -> RestoreLogResponse:
    del user
    record = await service.get(restore_id)
    if record.agent_task_id is None:
        raise NotFound(
            f"Restore #{restore_id} has no agent task, so there is no log to read — it never "
            "reached the host. The reason is on the restore itself."
        )

    log = await container.agent.get_task_log(record.agent_task_id, tail=tail)
    return RestoreLogResponse(
        restore_id=restore_id,
        task_id=log.task_id,
        lines=log.lines,
        truncated=log.truncated,
        total_lines=log.total_lines,
    )
