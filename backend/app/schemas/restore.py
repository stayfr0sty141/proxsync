"""Restore API models.

The request shape is deliberately explicit. Every destructive option — overwriting an
existing guest, stopping a running one, starting it afterwards — is a named boolean that
defaults to the safe value, so a client that omits a field can never widen what a restore is
allowed to do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import GuestType, RestoreMode, RestoreSource, RestoreStatus

CONFIRMATION_TOKEN_PREFIX = "rst_"  # noqa: S105 - a display prefix, not a credential


class PreflightCheck(BaseModel):
    """One named guard and what it found.

    `ok=False` is what makes a report *blocking*; `detail` carries the number or name an
    operator needs to act on it, which is the difference between "not enough space" and
    "412 GiB free, 71 GiB required".
    """

    name: str
    ok: bool
    detail: str | None = None


class PreflightReport(BaseModel):
    checks: list[PreflightCheck]
    blocking: bool
    """True when any check failed. A restore is never created from a blocking report."""
    warnings: list[str] = []
    """Things worth reading before confirming, none of which prevent the restore."""
    source: RestoreSource = RestoreSource.LOCAL
    """Where the archive will come from. `gdrive` means it is downloaded first."""
    backup_id: int
    filename: str | None = None
    size_bytes: int | None = None
    target_vmid: int
    target_type: GuestType
    target_storage: str
    target_node: str

    @property
    def failed(self) -> list[str]:
        return [check.name for check in self.checks if not check.ok]


class RestoreRequest(BaseModel):
    """The body of both `/restores/preflight` and `/restores` — the same decision, made twice.

    Preflight answers "would this work?" without writing anything; creating a restore answers
    the same question and then records the intent. Sharing one model is what guarantees the
    dialog an operator confirms describes the restore that was actually recorded.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    backup_id: int
    restore_mode: RestoreMode = RestoreMode.NEW_ID
    target_vmid: Annotated[int, Field(ge=100, le=999_999_999)] | None = None
    """Required for `new_id`; ignored for `original_id`, which uses the backup's own VMID."""
    target_storage: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    """Defaults to the configured default storage."""
    target_node: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    """Defaults to the node the agent runs on."""
    overwrite_existing: bool = False
    force_stop: bool = False
    start_after_restore: bool = False

    @model_validator(mode="after")
    def _target_matches_mode(self) -> RestoreRequest:
        if self.restore_mode is RestoreMode.NEW_ID and self.target_vmid is None:
            raise ValueError("restore_mode 'new_id' requires an explicit target_vmid")
        return self


class RestoreConfirmRequest(BaseModel):
    """Both fields are the confirmation.

    The token proves the request came from the dialog that showed the preflight report; the
    VMID proves the operator read which guest it names. Either one alone is a click.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    confirmation_token: Annotated[str, Field(min_length=8, max_length=128)]
    target_vmid: Annotated[int, Field(ge=100, le=999_999_999)]


class RestoreResponse(BaseModel):
    id: int
    backup_id: int
    source: RestoreSource
    filename: str | None = None
    source_vmid: int | None = None
    target_vmid: int
    target_type: GuestType
    restore_mode: RestoreMode
    target_node: str
    target_storage: str
    overwrite_existing: bool
    force_stop: bool
    start_after_restore: bool
    status: RestoreStatus
    confirmation_expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    preflight: PreflightReport | None = None
    agent_task_id: str | None = None
    requested_by: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    correlation_id: str
    created_at: datetime


class RestoreCreatedResponse(RestoreResponse):
    """The only response that ever carries the token.

    It is hashed on the way in and never read back, so losing this response means the restore
    can only be cancelled and requested again — which is the correct outcome, not a bug.
    """

    confirmation_token: str
    expires_at: datetime


class RestoreListResponse(BaseModel):
    items: list[RestoreResponse]
    total: int
    limit: int
    offset: int


class RestoreLogResponse(BaseModel):
    restore_id: int
    task_id: str
    lines: list[str]
    truncated: bool
    total_lines: int
