"""Retention policy, preview and execution response models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import GuestType, RetentionAction


class RetentionLocation(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class RetentionPreviewRequest(BaseModel):
    """Prospective values from the settings form.

    Both fields are optional so the endpoint can also preview the policy currently stored in
    the database. A preview is deliberately read-only even though it uses POST: the request
    body represents unsaved settings and is not a command to apply them.
    """

    model_config = ConfigDict(extra="forbid")

    keep_local: Annotated[int, Field(ge=1, le=365)] | None = None
    keep_remote: Annotated[int, Field(ge=0, le=365)] | None = None
    backup_id: Annotated[int, Field(gt=0)] | None = None


class RetentionPolicySnapshot(BaseModel):
    """The complete deletion policy used for one evaluation pass."""

    keep_local: int
    keep_remote: int
    retention_source: Literal["global", "job", "legacy_job", "unresolved"] = "global"
    policy_resolved: bool = True
    policy_block_reason: str | None = None
    policy_run_id: int | None = None
    policy_job_id: int | None = None
    scope: Literal["per_guest"] = "per_guest"
    require_upload_before_delete: Literal[True] = True
    dry_run: bool
    storage_warning_percent: int
    storage_critical_percent: int

    gdrive_enabled: bool
    delete_remote_on_retention: bool
    remote_name: str
    remote_folder: str

    trigger_backup_id: int | None = None


class RetentionDecision(BaseModel):
    """What retention decided for one artifact at one location."""

    backup_id: int
    vmid: int
    guest_type: GuestType
    guest_name: str
    filename: str
    location: RetentionLocation
    action: RetentionAction
    reason: str
    size_bytes: int

    would_delete: bool = False
    deleted: bool = False
    reconciled: bool = False
    freed_bytes: int = 0
    error: str | None = None


class RetentionSummary(BaseModel):
    evaluated: int = 0
    local_delete_count: int = 0
    local_delete_bytes: int = 0
    remote_delete_count: int = 0
    remote_delete_bytes: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    failed: int = 0


class RetentionResponse(BaseModel):
    policy: RetentionPolicySnapshot
    items: list[RetentionDecision]
    summary: RetentionSummary
