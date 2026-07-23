"""Backup job, run and history API models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestType,
    RunStatus,
    TargetSelector,
    TriggerType,
    UploadStatus,
)

MAX_TARGETS = 500
"""A guard against a pathological request, not a supported cluster size."""


class GuestTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vmid: Annotated[int, Field(ge=100, le=999_999_999)]
    """PVE reserves ids below 100."""
    guest_type: GuestType


def assert_distinct_vmids(targets: list[GuestTarget]) -> None:
    """Reject a target list naming the same VMID twice.

    Deduplicating on `(vmid, guest_type)` would not be enough: Proxmox VMIDs are unique
    across VMs *and* containers, so `vm 101` and `lxc 101` cannot both exist, and
    `backup_job_targets` enforces that with a `(job_id, vmid)` unique constraint. Catching it
    here makes it a 422 that explains the rule, instead of a 500 from the database.
    """
    seen: set[int] = set()
    for target in targets:
        if target.vmid in seen:
            raise ValueError(
                f"VMID {target.vmid} is listed more than once. Proxmox VMIDs are unique "
                "across VMs and containers, so a guest can only appear once."
            )
        seen.add(target.vmid)


class ResolvedTarget(BaseModel):
    """A target after it has been checked against the inventory and the allow-list."""

    guest_id: int
    vmid: int
    guest_type: GuestType
    guest_name: str
    node: str


# ---- jobs --------------------------------------------------------------------


class BackupJobBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=128)]
    enabled: bool = True
    cron_expression: Annotated[str, Field(min_length=1, max_length=64)] = "0 1 * * 0"
    timezone: str = "Asia/Jakarta"
    mode: BackupMode = BackupMode.SNAPSHOT
    compression: Compression = Compression.ZSTD
    zstd_threads: Annotated[int, Field(ge=0, le=64)] | None = None
    storage: Annotated[str, Field(min_length=1, max_length=64)] = "backup-hdd"
    target_selector: TargetSelector = TargetSelector.ALL
    targets: Annotated[list[GuestTarget], Field(max_length=MAX_TARGETS)] = []
    keep_local: Annotated[int, Field(ge=1, le=365)] = 2
    keep_remote: Annotated[int, Field(ge=0, le=365)] = 2
    upload_enabled: bool = True
    notify_on_start: bool = True
    notify_on_success: bool = True
    notify_on_failure: bool = True
    bwlimit_kbps: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    max_parallel: Annotated[int, Field(ge=1, le=8)] = 1

    @model_validator(mode="after")
    def _targets_match_selector(self) -> BackupJobBase:
        if self.target_selector is TargetSelector.ALL:
            if self.targets:
                raise ValueError(
                    "target_selector 'all' takes no explicit targets; "
                    "use 'include' or 'exclude' to name guests"
                )
        elif not self.targets:
            raise ValueError(
                f"target_selector '{self.target_selector.value}' requires at least one target"
            )

        assert_distinct_vmids(self.targets)
        return self


class BackupJobCreate(BackupJobBase):
    pass


class BackupJobUpdate(BackupJobBase):
    """A full replacement, not a patch.

    A schedule is small and safety-critical: a partial update where an omitted `targets` could
    mean either "no change" or "back up nothing" is exactly the ambiguity that silently
    widens a backup policy. The UI sends the whole object.
    """


class BackupJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    cron_expression: str
    timezone: str
    mode: BackupMode
    compression: Compression
    zstd_threads: int | None
    storage: str
    target_selector: TargetSelector
    targets: list[GuestTarget] = []
    keep_local: int
    keep_remote: int
    upload_enabled: bool
    notify_on_start: bool
    notify_on_success: bool
    notify_on_failure: bool
    bwlimit_kbps: int
    max_parallel: int
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BackupJobListResponse(BaseModel):
    items: list[BackupJobResponse]
    total: int


class JobPreviewResponse(BaseModel):
    job_id: int
    cron_expression: str
    timezone: str
    next_fire_times: list[datetime]
    resolved_targets: list[ResolvedTarget]
    skipped: list[str]
    """Named targets that would not run, with the reason — a disabled guest or one PVE no
    longer reports. Silence here would let a job appear healthy while backing up nothing."""


# ---- runs --------------------------------------------------------------------


class ManualBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: Annotated[list[GuestTarget], Field(min_length=1, max_length=MAX_TARGETS)]
    mode: BackupMode | None = None
    compression: Compression | None = None
    zstd_threads: Annotated[int, Field(ge=0, le=64)] | None = None
    storage: str | None = None
    bwlimit_kbps: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    upload: bool | None = None
    notes: Annotated[str, Field(max_length=255)] | None = None

    @model_validator(mode="after")
    def _unique_targets(self) -> ManualBackupRequest:
        assert_distinct_vmids(self.targets)
        return self


class RunOptions(BaseModel):
    """How a run will back up: resolved once, when the run is requested, then frozen.

    Persisted on `backup_runs.options`. Resolving at request time rather than execution time
    means editing a schedule while its run is queued cannot change what that run does, and
    history records the settings a backup actually used rather than today's defaults.
    """

    model_config = ConfigDict(extra="forbid")

    mode: BackupMode
    compression: Compression
    zstd_threads: Annotated[int, Field(ge=0, le=64)] | None = None
    storage: str
    bwlimit_kbps: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    upload: bool = False
    retention_source: Literal["global", "job"] = "global"
    """Where retention must resolve its effective keep counts.

    The explicit marker lets a scheduled run retain its frozen job policy even if the job is
    later deleted and its foreign key is set to NULL. Legacy rows do not contain this key and
    are resolved conservatively by the retention repository/service.
    """
    keep_local: Annotated[int, Field(ge=1, le=365)] = 2
    keep_remote: Annotated[int, Field(ge=0, le=365)] = 2
    notes: str | None = None


class RunProgress(BaseModel):
    """Live progress for the guest currently being backed up.

    Held in memory by the run executor, never persisted: writing a percentage to disk every
    few seconds is write amplification for data that is worthless a minute later.
    """

    backup_id: int | None = None
    vmid: int | None = None
    guest_name: str | None = None
    percent: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    rate_bps: int | None = None
    eta_seconds: int | None = None
    message: str | None = None


class BackupRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int | None
    job_name: str | None = None
    trigger: TriggerType
    status: RunStatus
    guest_total: int
    guest_succeeded: int
    guest_failed: int
    targets: list[dict[str, Any]] = []
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    error_message: str | None
    correlation_id: str
    created_at: datetime
    progress: RunProgress | None = None


class BackupRunListResponse(BaseModel):
    items: list[BackupRunResponse]
    total: int


class RunAcceptedResponse(BaseModel):
    run: BackupRunResponse
    queued_position: int | None = None
    """How many runs are ahead of this one. The agent serialises backups, so a run submitted
    while another is in flight waits rather than failing."""


# ---- history -----------------------------------------------------------------


class BackupHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None
    vmid: int
    guest_type: GuestType
    guest_name: str
    node: str
    storage: str
    filename: str | None
    local_path: str | None
    size_bytes: int | None
    mode: BackupMode
    compression: Compression
    checksum_sha256: str | None
    status: BackupStatus
    agent_task_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    error_message: str | None
    warnings: list[str] | None
    upload_status: UploadStatus
    uploaded_at: datetime | None
    remote_path: str | None
    retention_locked: bool
    local_deleted_at: datetime | None
    remote_deleted_at: datetime | None
    correlation_id: str | None
    created_at: datetime


class BackupHistoryListResponse(BaseModel):
    items: list[BackupHistoryResponse]
    total: int
    limit: int
    offset: int


class BackupLogResponse(BaseModel):
    backup_id: int
    task_id: str
    lines: list[str]
    truncated: bool
    total_lines: int


class BackupLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_locked: bool


class BackupDeleteResponse(BaseModel):
    backup_id: int
    deleted: list[str]
    freed_bytes: int
