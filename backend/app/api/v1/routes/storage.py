"""Storage status, history, forecast and per-guest consumption."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import RequireViewer, StorageServiceDep
from app.schemas.storage import (
    StorageByGuestResponse,
    StorageForecastResponse,
    StorageHistoryResponse,
    StorageStatusResponse,
)

router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("", summary="Live local and remote storage usage")
async def storage_status(service: StorageServiceDep, user: RequireViewer) -> StorageStatusResponse:
    del user
    return await service.live()


@router.get("/history", summary="Storage usage history")
async def storage_history(
    service: StorageServiceDep,
    user: RequireViewer,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> StorageHistoryResponse:
    del user
    return await service.history(days=days)


@router.get("/forecast", summary="Estimated time until local storage is full")
async def storage_forecast(
    service: StorageServiceDep, user: RequireViewer
) -> StorageForecastResponse:
    del user
    return await service.forecast()


@router.get("/by-guest", summary="Local backup space consumed per guest")
async def storage_by_guest(
    service: StorageServiceDep, user: RequireViewer
) -> StorageByGuestResponse:
    del user
    return await service.by_guest()
