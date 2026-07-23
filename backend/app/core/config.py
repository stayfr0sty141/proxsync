"""Dashboard configuration.

Secrets arrive from the environment and never from the database. `secret_key` is the single
root secret: the JWT signing key and the settings-encryption key are derived from it with
HKDF, so rotating one value rotates both and neither is stored anywhere.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PositiveInt = Annotated[int, Field(gt=0)]

MIN_SECRET_KEY_LENGTH = 32


def _split_csv(value: Any) -> Any:
    """Accept both JSON lists and comma-separated strings from the environment.

    pydantic-settings treats a ``list[...]`` field as "complex" and ``json.loads()`` it at the
    source level — for both environment variables and the ``.env`` file — *before* any
    ``mode="before"`` validator runs. A bare CSV value such as ``https://ui.lan`` is not valid
    JSON, so it would raise ``JSONDecodeError`` before this function was ever reached. The
    model sets ``enable_decoding=False`` to switch that source-level decode off, and this
    function owns *both* formats instead.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            return json.loads(stripped)
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROXSYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        # Off, so list fields are not json.loads()'d at the source before _split_csv runs.
        # This is what lets a bare CSV like CORS_ORIGINS=https://ui.lan load from .env.
        enable_decoding=False,
    )

    # ---- Service -------------------------------------------------------------
    environment: Literal["development", "production"] = "production"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    root_path: str = ""
    log_level: str = "INFO"
    log_json: bool = True

    # ---- Database ------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:////var/lib/proxsync/proxsync.db"
    """Async driver URL. PostgreSQL: postgresql+asyncpg://user:pass@host/proxsync"""
    database_echo: bool = False
    database_pool_size: _PositiveInt = 5
    database_max_overflow: int = 10

    # ---- Cryptography --------------------------------------------------------
    secret_key: SecretStr = SecretStr("")
    """Root secret, >=32 chars. Generate with: openssl rand -hex 32"""

    # ---- Authentication ------------------------------------------------------
    access_token_ttl_seconds: _PositiveInt = 15 * 60
    refresh_token_ttl_seconds: _PositiveInt = 7 * 24 * 3600
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "proxsync"
    jwt_audience: str = "proxsync-dashboard"

    cookie_secure: bool = True
    cookie_domain: str | None = None
    cookie_path: str = "/"

    login_max_attempts: _PositiveInt = 5
    login_attempt_window_seconds: _PositiveInt = 15 * 60
    login_lockout_base_seconds: _PositiveInt = 60
    login_lockout_max_seconds: _PositiveInt = 3600

    argon2_time_cost: _PositiveInt = 3
    argon2_memory_cost: _PositiveInt = 65536
    argon2_parallelism: _PositiveInt = 4

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr | None = None
    """Set once for first start. The account is created with must_change_password."""

    # ---- Frontend / CORS -----------------------------------------------------
    cors_origins: list[str] = Field(default_factory=list)
    """Empty when the frontend is served from the same origin, which is the default."""

    # ---- Backup Agent --------------------------------------------------------
    agent_base_url: str = "https://127.0.0.1:8765"
    agent_api_key_id: str = "proxsync-dashboard"
    agent_hmac_secret: SecretStr = SecretStr("")
    agent_ca_cert: Path | None = None
    agent_client_cert: Path | None = None
    agent_client_key: Path | None = None
    agent_verify_tls: bool = True
    agent_timeout_seconds: float = 30.0
    agent_connect_timeout_seconds: float = 5.0
    agent_max_retries: int = 2
    agent_circuit_failure_threshold: _PositiveInt = 3
    agent_circuit_reset_seconds: _PositiveInt = 30

    # ---- Proxmox (read-only inventory, decision D2) ---------------------------
    proxmox_base_url: str = "https://127.0.0.1:8006"
    proxmox_token_id: str = ""
    """e.g. proxsync@pve!dashboard — must hold PVEAuditor and nothing more."""
    proxmox_token_secret: SecretStr = SecretStr("")
    proxmox_verify_tls: bool = True
    proxmox_ca_cert: Path | None = None
    proxmox_node: str = "pve"
    proxmox_timeout_seconds: float = 15.0

    # ---- Background workers --------------------------------------------------
    background_workers: bool = True
    """Master switch for the run executor, the scheduler and the inventory refresh. Off in
    tests, so a suite never races a worker; off in a maintenance deployment, so an operator
    can inspect the database without anything firing."""

    scheduler_enabled: bool = True
    scheduler_misfire_grace_seconds: _PositiveInt = 3600
    """How late a missed schedule may still run after a restart. Beyond this the run is
    skipped and logged: a weekly backup starting three days late, unattended, is more
    surprising than one that did not start at all."""

    run_idle_poll_seconds: float = Field(default=5.0, gt=0)
    """How often the run executor re-checks the queue when the doorbell has not rung. The
    doorbell handles the normal case; this only bounds the worst case, where a notification
    arrived a moment before its transaction committed."""

    agent_unreachable_grace_seconds: _PositiveInt = 1800
    """How long a running backup may go unpolled before its outcome is recorded as
    `interrupted`. Not `failed`: the vzdump may well have succeeded, and claiming otherwise
    would be a lie the retention policy then acts on."""

    sync_idle_poll_seconds: float = Field(default=10.0, gt=0)
    """Also drives the retry schedule: a transfer waiting on backoff becomes ready without
    anything ringing a doorbell for it."""

    sync_retry_base_seconds: _PositiveInt = 30
    sync_retry_max_seconds: _PositiveInt = 3600
    """Exponential backoff with full jitter between upload attempts. Jittered because the
    common failure is a quota shared by every pending transfer, and retrying them in lockstep
    would reproduce the burst that caused it."""

    restore_idle_poll_seconds: float = Field(default=5.0, gt=0)
    """Backstop for the restore executor's doorbell, and the cadence of its expiry sweep."""

    restore_confirmation_ttl_seconds: _PositiveInt = 300
    """How long a restore may sit in `pending_confirmation` before it expires.

    Short on purpose. The token is what turns "I clicked restore" into "I meant this guest,
    now" — a window measured in hours would let a stale tab confirm a restore against an
    inventory that has since changed."""

    storage_sample_interval_seconds: float = Field(default=15 * 60, gt=0)
    """Storage history cadence. Kept in environment configuration so tests and maintenance
    deployments can shorten it without turning a UI preference into worker topology."""

    notification_idle_poll_seconds: float = Field(default=15.0, gt=0)
    """Backstop for the outbox doorbell, and what makes a scheduled retry become ready."""

    notification_max_attempts: _PositiveInt = 5
    notification_retry_base_seconds: _PositiveInt = 30
    notification_retry_max_seconds: _PositiveInt = 3600
    """Exponential backoff with full jitter between delivery attempts, as for uploads. A
    Telegram outage affects every pending message at once, so retrying them in lockstep would
    reproduce the burst."""

    notification_suppress_window_seconds: _PositiveInt = 300
    """How long an identical report is treated as a repeat rather than news.

    Only applies to events that can genuinely recur (a storage threshold, a retention pass).
    One-per-occurrence events are keyed uniquely instead and never depend on a clock."""

    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = 15.0

    # ---- Log persistence -----------------------------------------------------
    log_persist_enabled: bool = True
    """Whether structlog output is also written to the `logs` table for `/logs`."""

    log_persist_level: str = "INFO"
    """Floor for persistence only. Console output still honours `log_level`; the database is
    not the place for per-poll debug lines."""

    log_buffer_size: _PositiveInt = 2000
    """Bounded hand-off between the logging call and the writer. A full buffer drops its
    oldest entry — a backup must never block, or fail, because logging is behind."""

    log_flush_interval_seconds: float = Field(default=2.0, gt=0)
    log_flush_batch_size: _PositiveInt = 200
    log_prune_interval_seconds: float = Field(default=24 * 3600, gt=0)
    """Cadence of the retention pass over `logs` and `audit_events`; the ages themselves are
    operator settings (`general.log_retention_days`, `general.audit_retention_days`)."""

    events_queue_size: _PositiveInt = 200

    # ---- Defaults seeded into the settings table -----------------------------
    default_timezone: str = "Asia/Jakarta"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("log_level", "log_persist_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return level

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, value: str) -> str:
        if value.startswith(("sqlite:", "postgresql:", "postgres:")):
            raise ValueError(
                "database_url must name an async driver "
                "(sqlite+aiosqlite:// or postgresql+asyncpg://)"
            )
        return value

    @model_validator(mode="after")
    def _secret_key_is_strong(self) -> Settings:
        secret = self.secret_key.get_secret_value()
        if len(secret) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"PROXSYNC_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters. "
                "Generate one with: openssl rand -hex 32"
            )
        return self

    # ---- Derived -------------------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def sqlite_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        _, _, tail = self.database_url.partition("///")
        return Path("/" + tail.lstrip("/")) if tail else None

    @property
    def agent_configured(self) -> bool:
        return bool(self.agent_hmac_secret.get_secret_value())

    @property
    def proxmox_configured(self) -> bool:
        return bool(self.proxmox_token_id and self.proxmox_token_secret.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
