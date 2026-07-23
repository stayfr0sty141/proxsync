"""Storage reporting (decision D1/D2 extension endpoint)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.schemas.responses import StorageStatusResponse

router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/status", summary="Backup storage usage")
async def storage_status(container: ContainerDep) -> StorageStatusResponse:
    return await container.storage_service.status()
