"""Storage-monitor API models.

Missing upstream figures stay ``None``.  In particular, an unavailable filesystem or remote
must never be rendered as zero bytes: zero is a valid measurement, while unavailable is an
operational failure the UI needs to show explicitly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import GuestType


class StorageSeverity(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"


class StorageEntryResponse(BaseModel):
    """One row returned by ``pvesm status`` on the agent."""

    name: str
    type: str
    active: bool
    total_bytes: int
    used_bytes: int
    available_bytes: int
    used_percent: float


class LocalStorageResponse(BaseModel):
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float | None
    artifact_count: int
    artifact_bytes: int
    severity: StorageSeverity
    warning_percent: int
    critical_percent: int


class RemoteStorageResponse(BaseModel):
    remote: str
    configured: bool
    quota_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    trashed_bytes: int | None = None
    used_percent: float | None = None
    detail: str | None = None


class StorageStatusResponse(BaseModel):
    captured_at: datetime
    local: LocalStorageResponse | None
    local_detail: str | None = None
    storages: list[StorageEntryResponse] = Field(default_factory=list)
    remote: RemoteStorageResponse


class StorageSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    captured_at: datetime
    local_total_bytes: int
    local_used_bytes: int
    local_free_bytes: int
    local_used_percent: float | None
    severity: StorageSeverity
    remote_used_bytes: int | None
    remote_quota_bytes: int | None
    backup_count: int
    oldest_backup_at: datetime | None


class StorageHistoryResponse(BaseModel):
    days: int
    items: list[StorageSnapshotResponse]


class StorageForecastResponse(BaseModel):
    window_days: int = 30
    sample_count: int
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    growth_bytes_per_day: float | None = None
    days_until_full: float | None = None
    estimated_full_at: datetime | None = None
    detail: str | None = None


class GuestStorageResponse(BaseModel):
    vmid: int
    guest_type: GuestType
    guest_name: str
    backup_count: int
    total_bytes: int
    latest_backup_at: datetime | None


class StorageByGuestResponse(BaseModel):
    items: list[GuestStorageResponse]
    total_bytes: int
