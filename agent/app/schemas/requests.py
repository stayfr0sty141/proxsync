"""Request models.

Pydantic performs the first pass — types, ranges, string shapes. The validators in
:mod:`app.validators` perform the second, against the live host. Both must pass.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.enums import BackupMode, Compression, GuestType
from app.validators.identifiers import VMID_MAX, VMID_MIN

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_NOTES_ALLOWED = re.compile(r"^[\w \-.,:;/()\[\]@#=+']{0,512}$")

Vmid = Annotated[int, Field(ge=VMID_MIN, le=VMID_MAX)]
Bandwidth = Annotated[int, Field(ge=0, le=10_000_000, description="KiB/s; 0 disables the limit")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    correlation_id: str | None = None

    @field_validator("correlation_id")
    @classmethod
    def _valid_correlation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _CORRELATION_ID.match(value):
            raise ValueError("correlation_id must be 1-64 chars of [A-Za-z0-9._:-]")
        return value


class BackupStartRequest(_StrictModel):
    vmid: Vmid
    guest_type: GuestType
    mode: BackupMode = BackupMode.SNAPSHOT
    compression: Compression = Compression.ZSTD
    zstd_threads: Annotated[int, Field(ge=0, le=64)] | None = None
    """vzdump's ``--zstd`` worker count. 0 means half the host's cores. None keeps PVE's default."""
    storage: Annotated[str, Field(min_length=1, max_length=64)]
    bwlimit_kbps: Bandwidth = 0
    notes: Annotated[str, Field(max_length=512)] | None = None

    @field_validator("notes")
    @classmethod
    def _safe_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _NOTES_ALLOWED.match(value):
            raise ValueError("notes contain characters that are not permitted")
        return value


class _RestoreRequest(_StrictModel):
    archive: Annotated[str, Field(min_length=1, max_length=255)]
    """Basename only — path separators are rejected before the filesystem is touched."""
    target_vmid: Vmid
    storage: Annotated[str, Field(min_length=1, max_length=64)]
    overwrite: bool = False
    force_stop: bool = False
    start_after: bool = False
    bwlimit_kbps: Bandwidth = 0
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")] | None = None
    """When set, the agent verifies the artifact's digest before restoring."""


class RestoreVmRequest(_RestoreRequest):
    pass


class RestoreLxcRequest(_RestoreRequest):
    unprivileged: bool | None = None
    """None keeps the value recorded in the archive."""


class BackupListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vmid: Vmid | None = None
    guest_type: GuestType | None = None


# ---- Google Drive sync -------------------------------------------------------

RemoteName = Annotated[str, Field(min_length=1, max_length=64)]
RemotePath = Annotated[str, Field(max_length=1024)]


class _SyncRequest(_StrictModel):
    """Shared shape. The remote name and path are validated again in
    :mod:`app.validators.remotes` before they reach an argv list — Pydantic checks the
    *shape*, the validator checks what rclone would *do* with it."""

    remote: RemoteName
    remote_path: RemotePath = ""
    bwlimit_kbps: Bandwidth = 0


class SyncUploadRequest(_SyncRequest):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    """Basename of a local vzdump artifact."""
    transfers: Annotated[int, Field(ge=1, le=32)] | None = None
    verify_after: bool = False
    """Compare the uploaded copy against the local file before reporting success."""


class SyncDownloadRequest(_SyncRequest):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    transfers: Annotated[int, Field(ge=1, le=32)] | None = None
    overwrite: bool = False
    """Refused by default: a download that silently replaced a local artifact could destroy
    the only good copy of a backup."""


class SyncVerifyRequest(_SyncRequest):
    filename: Annotated[str, Field(min_length=1, max_length=255)]


class SyncDeleteRequest(_SyncRequest):
    filename: Annotated[str, Field(min_length=1, max_length=255)]


class SyncListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote: RemoteName
    remote_path: RemotePath = ""
