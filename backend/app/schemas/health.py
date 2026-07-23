"""Health and readiness models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ComponentStatus = Literal["ok", "degraded", "unavailable", "not_configured"]


class ComponentHealth(BaseModel):
    name: str
    status: ComponentStatus
    detail: str | None = None
    latency_ms: float | None = None
    metadata: dict[str, object] = {}


class HealthResponse(BaseModel):
    """Liveness. Deliberately free of detail — it is reachable without authentication."""

    status: Literal["ok"]
    version: str


class HealthDetailResponse(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str
    environment: str
    started_at: datetime
    uptime_seconds: float
    components: list[ComponentHealth]
