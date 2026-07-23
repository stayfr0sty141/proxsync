"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.api.deps import HealthServiceDep, RequireAdmin
from app.schemas.health import HealthDetailResponse, HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness")
async def health() -> HealthResponse:
    """Unauthenticated and deliberately uninformative — it exists for load balancers and
    systemd, not for describing the deployment to whoever asks."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/health/detail", summary="Component health")
async def health_detail(service: HealthServiceDep, user: RequireAdmin) -> HealthDetailResponse:
    del user
    return await service.detail()
