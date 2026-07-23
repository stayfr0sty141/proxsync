"""Typed mirrors of the Backup Agent's responses.

These models are the seam between the two services. If the agent's contract changes, it
breaks here — loudly, in one place — rather than somewhere deep in a service that assumed a
dictionary key existed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import Compression, GuestType, VerifyOutcome


class AgentTaskProgress(BaseModel):
    percent: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    rate_bps: int | None = None
    eta_seconds: int | None = None
    message: str | None = None


class AgentTaskAccepted(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    kind: str
    state: str
    created_at: datetime
    correlation_id: str | None = None


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    kind: str
    state: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    error: str | None = None
    correlation_id: str | None = None
    progress: AgentTaskProgress = AgentTaskProgress()
    result: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    log_path: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in {"success", "failed", "cancelled", "interrupted"}

    @property
    def succeeded(self) -> bool:
        return self.state == "success"


class AgentTaskLog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    lines: list[str]
    truncated: bool
    total_lines: int


class AgentArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

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


class AgentDeletedArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str
    deleted: list[str]
    freed_bytes: int


class AgentFilesystemUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float


class AgentStorageEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: str
    active: bool
    total_bytes: int
    used_bytes: int
    available_bytes: int
    used_percent: float


class AgentStorageStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dump_root: AgentFilesystemUsage
    storages: list[AgentStorageEntry] = []
    artifact_count: int = 0
    artifact_bytes: int = 0


class AgentRemoteEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    path: str
    size_bytes: int
    is_dir: bool
    modified_at: datetime | None = None
    md5: str | None = None
    hashes: dict[str, str] = {}


class AgentRemoteList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    remote: str
    remote_path: str
    entries: list[AgentRemoteEntry] = []


class AgentRemoteQuota(BaseModel):
    model_config = ConfigDict(extra="ignore")

    remote: str
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    trashed_bytes: int | None = None
    used_percent: float | None = None


class AgentVerifyResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

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


class AgentHealth(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    version: str
    hostname: str | None = None
    uptime_seconds: float | None = None
    dump_root: str | None = None
    dump_root_writable: bool | None = None
    binaries: dict[str, bool] = {}
    active_tasks: int = 0
    mtls_enabled: bool = False
