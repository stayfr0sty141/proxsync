"""Backup endpoints: start, list, delete."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import ContainerDep
from app.schemas.enums import GuestType
from app.schemas.requests import BackupStartRequest
from app.schemas.responses import (
    ArtifactResponse,
    DeletedArtifactResponse,
    TaskAcceptedResponse,
)
from app.validators.identifiers import VMID_MAX, VMID_MIN

router = APIRouter(prefix="/backup", tags=["backup"])


@router.post(
    "/start",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a vzdump backup",
)
async def start_backup(
    request: BackupStartRequest, container: ContainerDep
) -> TaskAcceptedResponse:
    task = await container.backup_service.start(request)
    return TaskAcceptedResponse(
        task_id=task.id,
        state=task.state,
        kind=task.kind,
        created_at=task.created_at,
        correlation_id=task.correlation_id,
    )


@router.get("/list", summary="List backup artifacts")
async def list_backups(
    container: ContainerDep,
    vmid: Annotated[int | None, Query(ge=VMID_MIN, le=VMID_MAX)] = None,
    guest_type: GuestType | None = None,
) -> list[ArtifactResponse]:
    return container.artifact_service.list(vmid=vmid, guest_type=guest_type)


@router.delete(
    "/{artifact_id}",
    summary="Delete a backup artifact and its sidecars",
)
async def delete_backup(
    container: ContainerDep,
    artifact_id: Annotated[str, Path(min_length=1, max_length=255)],
) -> DeletedArtifactResponse:
    return container.artifact_service.delete(artifact_id)
