"""Restore endpoints. Separate routes per guest type so the argv builders never branch on
caller-supplied data."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import ContainerDep
from app.schemas.requests import RestoreLxcRequest, RestoreVmRequest
from app.schemas.responses import TaskAcceptedResponse

router = APIRouter(prefix="/restore", tags=["restore"])


@router.post(
    "/vm",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Restore a QEMU virtual machine with qmrestore",
)
async def restore_vm(request: RestoreVmRequest, container: ContainerDep) -> TaskAcceptedResponse:
    task = await container.restore_service.restore_vm(request)
    return TaskAcceptedResponse(
        task_id=task.id,
        state=task.state,
        kind=task.kind,
        created_at=task.created_at,
        correlation_id=task.correlation_id,
    )


@router.post(
    "/lxc",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Restore an LXC container with pct restore",
)
async def restore_lxc(request: RestoreLxcRequest, container: ContainerDep) -> TaskAcceptedResponse:
    task = await container.restore_service.restore_lxc(request)
    return TaskAcceptedResponse(
        task_id=task.id,
        state=task.state,
        kind=task.kind,
        created_at=task.created_at,
        correlation_id=task.correlation_id,
    )
