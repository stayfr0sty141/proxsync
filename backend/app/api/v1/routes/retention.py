"""Read-only retention policy preview."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import RequireAdmin, RetentionServiceDep
from app.schemas.retention import RetentionPreviewRequest, RetentionResponse

router = APIRouter(prefix="/retention", tags=["retention"])


@router.post("/preview", summary="Preview retention without deleting or recording events")
async def preview_retention(
    payload: RetentionPreviewRequest,
    service: RetentionServiceDep,
    user: RequireAdmin,
) -> RetentionResponse:
    del user
    return await service.preview(payload)
