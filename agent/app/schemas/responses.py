"""Response models — the contract the dashboard's ``AgentClient`` is written against."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import Compression, GuestType, TaskKind, TaskState, VerifyOutcome
from app.tasks.models import Task


class TaskProgressResponse(BaseModel):
    percent: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    rate_bps: int | None = None
    eta_seconds: int | None = None
    message: str | None = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    kind: TaskKind
    state: TaskState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    error: str | None = None
    correlation_id: str | None = None
    progress: TaskProgressResponse
    result: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    log_path: str

    @classmethod
    def from_task(cls, task: Task) -> TaskResponse:
        return cls(
            task_id=task.id,
            kind=task.kind,
            state=task.state,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            duration_seconds=task.duration_seconds,
            exit_code=task.exit_code,
            error=task.error,
            correlation_id=task.correlation_id,
            progress=TaskProgressResponse(**task.progress.as_dict()),
            result=task.result,
            meta=task.meta,
            log_path=str(task.log_path),
        )


class TaskAcceptedResponse(BaseModel):
    task_id: str
    state: TaskState
    kind: TaskKind
    created_at: datetime
    correlation_id: str | None = None


class TaskLogResponse(BaseModel):
    task_id: str
    lines: list[str]
    truncated: bool
    total_lines: int


class ArtifactResponse(BaseModel):
    filename: str
    path: str
    vmid: int
    guest_type: GuestType
    size_bytes: int
    created_at: datetime
    modified_at: datetime
    compression: Compression
    checksum_sha256: str | None = None
    notes: str | None = None


class DeletedArtifactResponse(BaseModel):
    filename: str
    deleted: list[str]
    freed_bytes: int


class StorageEntryResponse(BaseModel):
    name: str
    type: str
    active: bool
    total_bytes: int
    used_bytes: int
    available_bytes: int
    used_percent: float


class FilesystemUsageResponse(BaseModel):
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float


class StorageStatusResponse(BaseModel):
    dump_root: FilesystemUsageResponse
    storages: list[StorageEntryResponse]
    artifact_count: int
    artifact_bytes: int


class RemoteEntryResponse(BaseModel):
    name: str
    path: str
    size_bytes: int
    is_dir: bool
    modified_at: datetime | None = None
    md5: str | None = None
    hashes: dict[str, str] = {}


class RemoteListResponse(BaseModel):
    remote: str
    remote_path: str
    entries: list[RemoteEntryResponse]


class RemoteQuotaResponse(BaseModel):
    remote: str
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    trashed_bytes: int | None = None
    used_percent: float | None = None
    """All optional: not every rclone backend reports a quota, and a missing figure must not
    render as 0, which the storage page would show as 'full'."""


class VerifyResultResponse(BaseModel):
    filename: str
    remote: str
    remote_path: str
    outcome: VerifyOutcome
    verified: bool
    local_size_bytes: int | None = None
    remote_size_bytes: int | None = None
    local_md5: str | None = None
    remote_md5: str | None = None
    detail: str | None = None


class SlotResponse(BaseModel):
    name: str
    in_use: int
    capacity: int


class HealthResponse(BaseModel):
    status: str
    version: str
    started_at: datetime
    uptime_seconds: float
    hostname: str
    dump_root: str
    dump_root_writable: bool
    binaries: dict[str, bool]
    slots: list[SlotResponse]
    active_tasks: int
    mtls_enabled: bool
