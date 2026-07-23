"""Closed vocabularies shared by requests, executors and the task registry."""

from __future__ import annotations

from enum import StrEnum


class GuestType(StrEnum):
    VM = "vm"
    LXC = "lxc"


class BackupMode(StrEnum):
    SNAPSHOT = "snapshot"
    SUSPEND = "suspend"
    STOP = "stop"


class Compression(StrEnum):
    ZSTD = "zstd"
    GZIP = "gzip"
    LZO = "lzo"
    NONE = "none"

    @property
    def vzdump_value(self) -> str:
        """vzdump spells gzip as ``gzip`` and "no compression" as ``0``."""
        return "0" if self is Compression.NONE else self.value


class TaskKind(StrEnum):
    BACKUP = "backup"
    RESTORE_VM = "restore_vm"
    RESTORE_LXC = "restore_lxc"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    VERIFY = "verify"


class VerifyOutcome(StrEnum):
    """Why a local artifact and its remote copy do or do not agree."""

    MATCH = "match"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    MISSING_REMOTE = "missing_remote"
    HASH_UNAVAILABLE = "hash_unavailable"
    """Sizes agree, but the remote publishes no hash to compare. Reported honestly rather
    than counted as verified — a truncated file that happens to be the right length would
    otherwise pass."""

    @property
    def verified(self) -> bool:
        return self is VerifyOutcome.MATCH


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    """The agent restarted while the task was running; the outcome is unknown."""

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.SUCCESS,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.INTERRUPTED,
        }
