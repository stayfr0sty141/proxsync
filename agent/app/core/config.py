"""Agent configuration.

Every value that influences what the agent is allowed to execute lives here and is loaded
once at startup. Nothing in a request may widen these bounds.
"""

from __future__ import annotations

import ipaddress
import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PositiveInt = Annotated[int, Field(gt=0)]


def _split_csv(value: Any) -> Any:
    """Accept both JSON lists and comma-separated strings from the environment.

    pydantic-settings treats a ``list[...]`` field as "complex" and ``json.loads()`` it at the
    source level — for both environment variables and the ``.env`` file — *before* any
    ``mode="before"`` validator runs. A bare CSV value such as ``backup-hdd`` or
    ``10.0.0.20/32`` is not valid JSON, so it would raise ``JSONDecodeError`` before this
    function was ever reached. The model sets ``enable_decoding=False`` to switch that
    source-level decode off, and this function owns *both* formats instead.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            return json.loads(stripped)
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROXSYNC_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        # Off, so list fields are not json.loads()'d at the source before _split_csv runs.
        # This is what lets a bare CSV like ALLOWED_BACKUP_STORAGES=backup-hdd load from .env.
        enable_decoding=False,
    )

    # ---- Service -------------------------------------------------------------
    bind_host: str = "0.0.0.0"  # noqa: S104 - restricted by allowed_client_networks + firewall
    bind_port: int = 8765
    log_level: str = "INFO"
    log_json: bool = True

    # ---- Transport security --------------------------------------------------
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    tls_client_ca: Path | None = None
    """When set, uvicorn requires and verifies a client certificate signed by this CA (mTLS)."""

    allowed_client_networks: list[str] = Field(default_factory=lambda: ["127.0.0.1/32"])
    """CIDR allow-list for the dashboard LXC. Enforced before authentication."""

    # ---- Request signing -----------------------------------------------------
    api_key_id: str = "proxsync-dashboard"
    hmac_secret: SecretStr = SecretStr("")
    signature_window_seconds: _PositiveInt = 60
    nonce_cache_size: _PositiveInt = 8192

    # ---- Filesystem ----------------------------------------------------------
    dump_root: Path = Path("/mnt/backup-hdd/dump")
    temp_dir: Path = Path("/mnt/backup-hdd/tmp")
    state_dir: Path = Path("/var/lib/proxsync-agent")
    log_dir: Path = Path("/var/log/proxsync-agent")

    qemu_config_dir: Path = Path("/etc/pve/qemu-server")
    lxc_config_dir: Path = Path("/etc/pve/lxc")

    # ---- Executables ---------------------------------------------------------
    # Absolute paths only: never resolved through PATH, which the caller could influence.
    vzdump_bin: Path = Path("/usr/bin/vzdump")
    qmrestore_bin: Path = Path("/usr/sbin/qmrestore")
    qm_bin: Path = Path("/usr/sbin/qm")
    pct_bin: Path = Path("/usr/sbin/pct")
    pvesm_bin: Path = Path("/usr/sbin/pvesm")
    rclone_bin: Path = Path("/usr/bin/rclone")

    # ---- Google Drive sync (decision D1: rclone runs here, not in the LXC) ----
    rclone_config: Path | None = Path("/var/lib/proxsync-agent/rclone.conf")
    """Read by rclone as root. The agent never parses it — it holds OAuth tokens, and the
    agent has no reason to see them."""

    allowed_remotes: list[str] = Field(default_factory=list)
    """Empty means "any well-formed remote name". Naming remotes here is strongly advised:
    it is what stops a request from addressing a *local* rclone remote and turning a copy
    into an arbitrary file read."""

    sync_enabled: bool = True
    max_concurrent_syncs: _PositiveInt = 1
    sync_timeout_seconds: _PositiveInt = 60 * 60 * 12
    rclone_transfers: Annotated[int, Field(ge=1, le=32)] = 4
    rclone_checkers: Annotated[int, Field(ge=1, le=64)] = 8
    rclone_low_level_retries: Annotated[int, Field(ge=0, le=20)] = 3
    rclone_stats_interval_seconds: _PositiveInt = 5

    # ---- Policy --------------------------------------------------------------
    allowed_vmids: list[int] = Field(default_factory=list)
    """Empty means "any guest that actually exists on this host"."""

    allowed_backup_storages: list[str] = Field(default_factory=lambda: ["backup-hdd"])
    allowed_restore_storages: list[str] = Field(default_factory=list)
    """Empty means "any storage reported by pvesm"."""

    verify_storage_with_pvesm: bool = True
    storage_cache_ttl_seconds: _PositiveInt = 30

    max_concurrent_backups: _PositiveInt = 1
    max_concurrent_restores: _PositiveInt = 1

    backup_timeout_seconds: _PositiveInt = 60 * 60 * 12
    restore_timeout_seconds: _PositiveInt = 60 * 60 * 12
    command_timeout_seconds: _PositiveInt = 60
    cancel_grace_seconds: _PositiveInt = 30

    checksum_after_backup: bool = True
    checksum_chunk_bytes: _PositiveInt = 4 * 1024 * 1024

    task_retention_hours: _PositiveInt = 24 * 14
    max_log_tail_lines: _PositiveInt = 5000

    @field_validator(
        "allowed_client_networks",
        "allowed_backup_storages",
        "allowed_restore_storages",
        "allowed_remotes",
        mode="before",
    )
    @classmethod
    def _csv_str(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("allowed_vmids", mode="before")
    @classmethod
    def _csv_int(cls, value: Any) -> Any:
        parsed = _split_csv(value)
        if isinstance(parsed, list):
            return [int(item) for item in parsed]
        return parsed

    @field_validator("allowed_client_networks")
    @classmethod
    def _valid_networks(cls, value: list[str]) -> list[str]:
        for network in value:
            ipaddress.ip_network(network, strict=False)
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return level

    # ---- Derived -------------------------------------------------------------
    @property
    def task_journal_dir(self) -> Path:
        return self.state_dir / "tasks"

    @property
    def task_log_dir(self) -> Path:
        return self.log_dir / "tasks"

    @property
    def client_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(n, strict=False) for n in self.allowed_client_networks)

    @property
    def mtls_enabled(self) -> bool:
        return self.tls_client_ca is not None

    def ensure_directories(self) -> None:
        for directory in (self.state_dir, self.task_journal_dir, self.log_dir, self.task_log_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    return AgentSettings()
