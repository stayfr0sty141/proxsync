"""Settings sections.

Each section is a Pydantic model; the settings table stores one row per field. The model is
the schema — validation, defaults and documentation live here rather than being scattered
across the UI and the services that read them.

**Connection credentials are not here.** The agent's URL, HMAC secret and client certificate,
the Proxmox API token, and the database URL all come from the environment. They are bootstrap
credentials: putting them in a database that the application needs those credentials to reach
is circular, and it would mean a database dump contained the keys to the host. These sections
hold operational preferences only.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.enums import BackupMode, Compression, SettingsSection

SECRET_UNCHANGED = "__unchanged__"  # noqa: S105 - a sentinel, not a credential
"""Sent back by the UI to mean "keep the stored secret". Never a real value."""


class SectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_default=True)


class GeneralSettings(SectionModel):
    timezone: str = "Asia/Jakarta"
    storage_path: str = "/mnt/backup-hdd"
    backup_folder: str = "dump"
    temp_folder: str = "/mnt/backup-hdd/tmp"
    log_retention_days: Annotated[int, Field(ge=1, le=3650)] = 90
    audit_retention_days: Annotated[int, Field(ge=30, le=3650)] = 730

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"'{value}' is not a known IANA timezone") from exc
        return value

    @field_validator("storage_path", "temp_folder")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("must be an absolute path")
        return value.rstrip("/") or "/"

    @field_validator("backup_folder")
    @classmethod
    def _relative_folder(cls, value: str) -> str:
        if value.startswith("/") or ".." in value:
            raise ValueError("must be a relative folder name without '..'")
        return value.strip("/")


class GDriveSettings(SectionModel):
    enabled: bool = False
    remote_name: str = "gdrive"
    folder: str = "proxsync/dump"
    bandwidth_limit_kbps: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    transfers: Annotated[int, Field(ge=1, le=32)] = 4
    upload_retry: Annotated[int, Field(ge=0, le=10)] = 3
    verify_after_upload: bool = True
    delete_remote_on_retention: bool = True

    @field_validator("remote_name")
    @classmethod
    def _rclone_remote(cls, value: str) -> str:
        cleaned = value.rstrip(":")
        if not cleaned or not all(char.isalnum() or char in "._-" for char in cleaned):
            raise ValueError("remote name may contain only letters, digits, '.', '_' and '-'")
        return cleaned

    @field_validator("folder")
    @classmethod
    def _remote_folder(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("folder must not contain '..'")
        return value.strip("/")


class TelegramSettings(SectionModel):
    enabled: bool = False
    bot_token: str | None = None
    """Write-only. Reads return None; the API reports `configured` and a masked hint."""
    chat_id: str = ""

    notify_backup_started: bool = True
    notify_backup_success: bool = True
    notify_backup_failed: bool = True
    notify_restore_started: bool = True
    notify_restore_finished: bool = True
    notify_restore_failed: bool = True
    notify_upload_failed: bool = True
    notify_retention_deleted: bool = False
    notify_storage_threshold: bool = True

    @field_validator("chat_id")
    @classmethod
    def _chat_id_shape(cls, value: str) -> str:
        if not value:
            return value
        candidate = value.removeprefix("-")
        if not (candidate.isdigit() or value.startswith("@")):
            raise ValueError("chat_id must be a numeric id (optionally negative) or an @channel")
        return value


class RetentionSettings(SectionModel):
    keep_local: Annotated[int, Field(ge=1, le=365)] = 2
    keep_remote: Annotated[int, Field(ge=0, le=365)] = 2
    scope: Literal["per_guest"] = "per_guest"
    """Retention is per VM and per LXC, never global. The literal type makes that a schema
    guarantee rather than a convention someone can quietly change."""
    require_upload_before_delete: Literal[True] = True
    """A hard safety invariant, not a feature flag.

    Keeping the field in the API makes the policy explicit and preserves compatibility with
    stored settings, while the literal type prevents an operator or stale client from disabling
    the upload guard that retention relies on to avoid deleting the only valid copy.
    """
    dry_run: bool = False
    storage_warning_percent: Annotated[int, Field(ge=50, le=99)] = 85
    storage_critical_percent: Annotated[int, Field(ge=51, le=100)] = 95

    @field_validator("storage_critical_percent")
    @classmethod
    def _critical_above_warning(cls, value: int, info: object) -> int:
        data = getattr(info, "data", {})
        warning = data.get("storage_warning_percent")
        if warning is not None and value <= warning:
            raise ValueError("critical threshold must be above the warning threshold")
        return value


class AgentSettings(SectionModel):
    """Operational preferences for talking to the Backup Agent.

    Connection details (URL, key id, HMAC secret, certificates) come from the environment.
    """

    poll_interval_seconds: Annotated[int, Field(ge=1, le=300)] = 5
    default_storage: str = "backup-hdd"
    default_mode: BackupMode = BackupMode.SNAPSHOT
    default_compression: Compression = Compression.ZSTD
    default_zstd_threads: Annotated[int, Field(ge=0, le=64)] | None = None
    default_bwlimit_kbps: Annotated[int, Field(ge=0, le=10_000_000)] = 0


class ProxmoxSettings(SectionModel):
    """Operational preferences for the read-only inventory client.

    The API token lives in the environment and must hold `PVEAuditor` and nothing more.
    """

    node: str = "pve"
    inventory_refresh_seconds: Annotated[int, Field(ge=30, le=86400)] = 300
    auto_enable_new_guests: bool = False
    """When false, a guest discovered on the host is *not* backed up until an admin enables
    it. Defaulting to false means adding a VM never silently changes what gets backed up."""


SECTION_MODELS: dict[SettingsSection, type[SectionModel]] = {
    SettingsSection.GENERAL: GeneralSettings,
    SettingsSection.GDRIVE: GDriveSettings,
    SettingsSection.TELEGRAM: TelegramSettings,
    SettingsSection.RETENTION: RetentionSettings,
    SettingsSection.AGENT: AgentSettings,
    SettingsSection.PROXMOX: ProxmoxSettings,
}

SECRET_FIELDS: dict[SettingsSection, frozenset[str]] = {
    SettingsSection.TELEGRAM: frozenset({"bot_token"}),
}


def secret_fields(section: SettingsSection) -> frozenset[str]:
    return SECRET_FIELDS.get(section, frozenset())


class SecretStatus(BaseModel):
    configured: bool
    hint: str | None = None
    updated_at: str | None = None


class SectionResponse(BaseModel):
    section: SettingsSection
    values: dict[str, object]
    """Secrets are omitted from `values` and described in `secrets` instead."""
    secrets: dict[str, SecretStatus] = {}


class AllSettingsResponse(BaseModel):
    sections: list[SectionResponse]
