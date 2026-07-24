"""Sync and backup-browser API models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.schemas.enums import GuestType, SyncDirection, SyncStatus, UploadStatus, VerifyOutcome


class SyncTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    backup_id: int | None
    filename: str | None = None
    direction: SyncDirection
    status: SyncStatus
    agent_task_id: str | None
    remote_name: str | None
    remote_path: str | None
    bytes_total: int | None
    bytes_transferred: int
    transfer_rate_bps: int | None
    percent: float | None = None
    attempt: int
    max_attempts: int
    next_retry_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def operation(self) -> str:
        return self.direction.value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bytes_done(self) -> int:
        return self.bytes_transferred

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rate_bps(self) -> int | None:
        return self.transfer_rate_bps


class SyncTaskListResponse(BaseModel):
    items: list[SyncTaskResponse]
    total: int


class SyncQueueStatus(BaseModel):
    """What the sync worker is doing, for the dashboard tile."""

    enabled: bool
    remote_name: str
    remote_path: str
    queued: int
    running: int
    failed: int
    pending_uploads: int
    """Successful backups awaiting an upload slot."""
    current: SyncTaskResponse | None = None


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    """Re-upload an artifact already marked uploaded — for a copy known to be bad."""


class VerifyResponse(BaseModel):
    backup_id: int
    filename: str
    outcome: VerifyOutcome
    verified: bool
    local_size_bytes: int | None = None
    remote_size_bytes: int | None = None
    local_md5: str | None = None
    remote_md5: str | None = None
    detail: str | None = None
    checked_at: datetime


class RemoteQuotaResponse(BaseModel):
    remote: str
    configured: bool
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    trashed_bytes: int | None = None
    used_percent: float | None = None
    detail: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quota_bytes(self) -> int | None:
        return self.total_bytes


# ---- browser -----------------------------------------------------------------


class BrowserEntry(BaseModel):
    filename: str
    vmid: int | None = None
    guest_type: GuestType | None = None
    size_bytes: int | None = None
    created_at: datetime | None = None
    checksum_sha256: str | None = None
    md5: str | None = None
    backup_id: int | None = None
    """Set when ProxSync's history knows this artifact; None for a file it did not create."""


class BrowserListResponse(BaseModel):
    location: str
    entries: list[BrowserEntry]
    total: int


class ComparisonState(BaseModel):
    filename: str
    state: Annotated[str, Field(pattern=r"^(in_sync|local_only|remote_only|size_mismatch)$")]
    local_size_bytes: int | None = None
    remote_size_bytes: int | None = None
    backup_id: int | None = None
    upload_status: UploadStatus | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def name(self) -> str:
        return self.filename

    @computed_field  # type: ignore[prop-decorator]
    @property
    def local_size(self) -> int | None:
        return self.local_size_bytes

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remote_size(self) -> int | None:
        return self.remote_size_bytes


class ComparisonResponse(BaseModel):
    remote: str
    remote_path: str
    in_sync: int
    local_only: int
    remote_only: int
    size_mismatch: int
    entries: list[ComparisonState]
    detail: str | None = None
    """Set when the comparison could not be completed — an unreachable remote, say — so a
    partial result is never mistaken for "everything is in sync"."""
