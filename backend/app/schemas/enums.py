"""Closed vocabularies.

Every one of these is stored as a VARCHAR with a CHECK constraint rather than a native ENUM
type: PostgreSQL enums are painful to alter and have no SQLite equivalent, so the constraint
lives in a migration and the meaning lives here.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return {UserRole.VIEWER: 0, UserRole.OPERATOR: 1, UserRole.ADMIN: 2}[self]

    def can_act_as(self, required: UserRole) -> bool:
        return self.rank >= required.rank


class GuestType(StrEnum):
    VM = "vm"
    LXC = "lxc"


class GuestStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class BackupMode(StrEnum):
    SNAPSHOT = "snapshot"
    SUSPEND = "suspend"
    STOP = "stop"


class Compression(StrEnum):
    ZSTD = "zstd"
    GZIP = "gzip"
    LZO = "lzo"
    NONE = "none"


class TargetSelector(StrEnum):
    ALL = "all"
    INCLUDE = "include"
    EXCLUDE = "exclude"


class TriggerType(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    API = "api"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED_AGENT_UNAVAILABLE = "skipped_agent_unavailable"

    @property
    def is_terminal(self) -> bool:
        return self not in {RunStatus.QUEUED, RunStatus.RUNNING}


class BackupStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    """Stopped on purpose. Distinct from `failed` because the dashboard's "Failed Jobs"
    counter must not include backups an operator deliberately stopped."""
    INTERRUPTED = "interrupted"
    """The agent restarted mid-backup; the outcome is unknown and must not be trusted."""
    DELETED = "deleted"

    @property
    def is_terminal(self) -> bool:
        return self is not BackupStatus.RUNNING


class UploadStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"


class VerifyOutcome(StrEnum):
    """Why a local artifact and its remote copy do or do not agree. Mirrors the agent's
    vocabulary exactly — a value the agent can return that the dashboard cannot name would
    be a silent contract break."""

    MATCH = "match"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    MISSING_REMOTE = "missing_remote"
    HASH_UNAVAILABLE = "hash_unavailable"
    """Sizes agree, but the remote publishes no hash. Not counted as verified: a truncated
    file of exactly the right length would otherwise pass."""

    @property
    def verified(self) -> bool:
        return self is VerifyOutcome.MATCH


class SyncDirection(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    VERIFY = "verify"


class SyncStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RestoreSource(StrEnum):
    LOCAL = "local"
    GDRIVE = "gdrive"


class RestoreMode(StrEnum):
    ORIGINAL_ID = "original_id"
    NEW_ID = "new_id"


class RestoreStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    """Requested and preflighted, but not yet permitted to run."""
    CONFIRMED = "confirmed"
    """The token was echoed back; the restore is queued for the executor."""
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    """Contact was lost while the restore was in flight, so the outcome is unknown.

    Not `failed`: `qmrestore` and `pct restore` destroy and recreate the target, so telling an
    operator that nothing happened is the one answer that could get a guest overwritten twice.
    """
    EXPIRED = "expired"
    """The confirmation window closed before anyone confirmed it."""

    @property
    def is_terminal(self) -> bool:
        return self not in {
            RestoreStatus.PENDING_CONFIRMATION,
            RestoreStatus.CONFIRMED,
            RestoreStatus.RUNNING,
        }

    @property
    def blocks_retention(self) -> bool:
        """States in which the source artifact must not be deleted underneath the restore."""
        return self in {
            RestoreStatus.PENDING_CONFIRMATION,
            RestoreStatus.CONFIRMED,
            RestoreStatus.RUNNING,
        }


class RetentionAction(StrEnum):
    EVALUATED = "evaluated"
    KEPT = "kept"
    DELETED_LOCAL = "deleted_local"
    DELETED_REMOTE = "deleted_remote"
    SKIPPED = "skipped"
    FAILED = "failed"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogCategory(StrEnum):
    API = "api"
    BACKUP = "backup"
    RESTORE = "restore"
    UPLOAD = "upload"
    RETENTION = "retention"
    SCHEDULER = "scheduler"
    AUTH = "auth"
    AGENT = "agent"
    NOTIFY = "notify"
    """Outbox and delivery. Its own category because "was the operator told?" is a different
    question from "did the backup work?", and answering it should not mean reading `system`."""
    SYSTEM = "system"


class NotificationChannel(StrEnum):
    TELEGRAM = "telegram"


class NotificationEvent(StrEnum):
    BACKUP_STARTED = "backup_started"
    BACKUP_SUCCESS = "backup_success"
    BACKUP_FAILED = "backup_failed"
    RESTORE_STARTED = "restore_started"
    RESTORE_FINISHED = "restore_finished"
    RESTORE_FAILED = "restore_failed"
    UPLOAD_FAILED = "upload_failed"
    RETENTION_DELETED = "retention_deleted"
    STORAGE_THRESHOLD = "storage_threshold"
    TEST = "test"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    """Deliberately not delivered because an identical message is already in flight or was
    just sent. Recorded rather than dropped: "we chose not to tell you" is information."""

    @property
    def is_terminal(self) -> bool:
        return self is not NotificationStatus.PENDING


class SettingsSection(StrEnum):
    GENERAL = "general"
    GDRIVE = "gdrive"
    TELEGRAM = "telegram"
    RETENTION = "retention"
    AGENT = "agent"
    PROXMOX = "proxmox"


class SettingValueType(StrEnum):
    STRING = "string"
    INT = "int"
    BOOL = "bool"
    JSON = "json"
    SECRET = "secret"  # noqa: S105 - an enum label, not a credential


class AuditAction(StrEnum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    TOKEN_REFRESH = "token_refresh"  # noqa: S105 - an enum label, not a credential
    TOKEN_REUSE_DETECTED = "token_reuse_detected"  # noqa: S105 - an enum label, not a credential
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 - an enum label, not a credential
    ACCOUNT_LOCKED = "account_locked"
    SETTINGS_CHANGED = "settings_changed"
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_DELETED = "user_deleted"
    BACKUP_STARTED = "backup_started"
    BACKUP_DELETED = "backup_deleted"
    RESTORE_REQUESTED = "restore_requested"
    RESTORE_CONFIRMED = "restore_confirmed"
    RESTORE_CANCELLED = "restore_cancelled"
    RETENTION_OVERRIDE = "retention_override"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
