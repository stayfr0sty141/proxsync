"""Backup jobs, runs, artifacts, transfers, restores and retention decisions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, enum_check, utcnow
from app.db.types import BigIntForeignKey, JsonDict, JsonList
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    RetentionAction,
    RunStatus,
    SyncDirection,
    SyncStatus,
    TargetSelector,
    TriggerType,
    UploadStatus,
)


class BackupJob(IdMixin, TimestampMixin, Base):
    __tablename__ = "backup_jobs"
    __table_args__ = (
        enum_check("mode", BackupMode, constraint="mode_valid"),
        enum_check("compression", Compression, constraint="compression_valid"),
        enum_check("target_selector", TargetSelector, constraint="target_selector_valid"),
        CheckConstraint("keep_local >= 1", name="ck_backup_jobs_keep_local_positive"),
        CheckConstraint("keep_remote >= 0", name="ck_backup_jobs_keep_remote_positive"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    cron_expression: Mapped[str] = mapped_column(String(64), default="0 1 * * 0")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Jakarta")

    mode: Mapped[str] = mapped_column(String(16), default=BackupMode.SNAPSHOT.value)
    compression: Mapped[str] = mapped_column(String(16), default=Compression.ZSTD.value)
    zstd_threads: Mapped[int | None] = mapped_column(Integer, default=None)
    """vzdump ``--zstd`` worker count; 0 = half the host's cores. PVE has no compression
    *level* setting, so ProxSync does not pretend to offer one."""
    storage: Mapped[str] = mapped_column(String(64), default="backup-hdd")

    target_selector: Mapped[str] = mapped_column(String(16), default=TargetSelector.ALL.value)

    keep_local: Mapped[int] = mapped_column(Integer, default=2)
    keep_remote: Mapped[int] = mapped_column(Integer, default=2)
    upload_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    notify_on_start: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_success: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_failure: Mapped[bool] = mapped_column(Boolean, default=True)

    bwlimit_kbps: Mapped[int] = mapped_column(Integer, default=0)
    max_parallel: Mapped[int] = mapped_column(Integer, default=1)

    last_run_at: Mapped[datetime | None] = mapped_column(default=None)
    next_run_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    created_by: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    targets: Mapped[list[BackupJobTarget]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="raise"
    )


class BackupJobTarget(IdMixin, Base):
    __tablename__ = "backup_job_targets"
    __table_args__ = (
        UniqueConstraint("job_id", "vmid", name="uq_backup_job_targets_job_vmid"),
        enum_check("guest_type", GuestType, constraint="guest_type_valid"),
    )

    job_id: Mapped[int] = mapped_column(
        BigIntForeignKey, ForeignKey("backup_jobs.id", ondelete="CASCADE"), index=True
    )
    vmid: Mapped[int] = mapped_column(Integer)
    guest_type: Mapped[str] = mapped_column(String(8))

    job: Mapped[BackupJob] = relationship(back_populates="targets", lazy="raise")


class BackupRun(IdMixin, Base):
    """One execution of a job, or one manual trigger."""

    __tablename__ = "backup_runs"
    __table_args__ = (
        Index("ix_backup_runs_status_started", "status", "started_at"),
        enum_check("trigger", TriggerType, constraint="trigger_valid"),
        enum_check("status", RunStatus, constraint="status_valid"),
    )

    job_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey,
        ForeignKey("backup_jobs.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(String(16), default=TriggerType.MANUAL.value)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.QUEUED.value)
    requested_by: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    guest_total: Mapped[int] = mapped_column(Integer, default=0)
    guest_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    guest_failed: Mapped[int] = mapped_column(Integer, default=0)

    targets: Mapped[JsonList | None] = mapped_column(default=None)
    """The resolved plan, e.g. ``[{"vmid": 101, "guest_type": "vm", "guest_name": "web"}]``.

    Recorded when the run is created rather than derived at read time, so "why was guest 103
    not backed up?" still has an answer after the guest is removed from Proxmox or the job's
    target list is edited."""

    options: Mapped[JsonDict | None] = mapped_column(default=None)
    """Mode, compression, storage and bandwidth as resolved when the run was *requested*.

    Frozen here rather than read from the job at execution time so that editing a schedule
    while its run is queued cannot change what that run does, and so history shows the
    settings a backup actually used."""

    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BackupHistory(IdMixin, TimestampMixin, Base):
    """One backup artifact. The central table of the application."""

    __tablename__ = "backup_history"
    __table_args__ = (
        # The retention and history queries: newest-first per guest.
        Index("ix_backup_history_guest_started", "vmid", "guest_type", "started_at"),
        Index("ix_backup_history_status", "status"),
        Index("ix_backup_history_upload_status", "upload_status"),
        UniqueConstraint("storage", "filename", name="uq_backup_history_storage_filename"),
        enum_check("guest_type", GuestType, constraint="guest_type_valid"),
        enum_check("mode", BackupMode, constraint="mode_valid"),
        enum_check("compression", Compression, constraint="compression_valid"),
        enum_check("status", BackupStatus, constraint="status_valid"),
        enum_check("upload_status", UploadStatus, constraint="upload_status_valid"),
    )

    run_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey,
        ForeignKey("backup_runs.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    guest_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("guests.id", ondelete="SET NULL"), default=None
    )

    # Denormalised so history survives the guest being deleted from Proxmox.
    vmid: Mapped[int] = mapped_column(Integer)
    guest_type: Mapped[str] = mapped_column(String(8))
    guest_name: Mapped[str] = mapped_column(String(128))
    node: Mapped[str] = mapped_column(String(64))
    storage: Mapped[str] = mapped_column(String(64))

    filename: Mapped[str | None] = mapped_column(String(255), default=None)
    local_path: Mapped[str | None] = mapped_column(String(512), default=None)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    mode: Mapped[str] = mapped_column(String(16), default=BackupMode.SNAPSHOT.value)
    compression: Mapped[str] = mapped_column(String(16), default=Compression.ZSTD.value)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[str] = mapped_column(String(16), default=BackupStatus.RUNNING.value)
    agent_task_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    """The agent's task id. CLI-invoked vzdump produces no PVE UPID, so this is the only
    correlation handle for the running process."""

    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    warnings: Mapped[JsonList | None] = mapped_column(default=None)
    log_path: Mapped[str | None] = mapped_column(String(512), default=None)

    upload_status: Mapped[str] = mapped_column(String(16), default=UploadStatus.PENDING.value)
    uploaded_at: Mapped[datetime | None] = mapped_column(default=None)
    remote_path: Mapped[str | None] = mapped_column(String(512), default=None)
    remote_size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    remote_checksum: Mapped[str | None] = mapped_column(String(64), default=None)

    retention_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    """Pinned by an operator: retention will never delete this artifact."""
    local_deleted_at: Mapped[datetime | None] = mapped_column(default=None)
    remote_deleted_at: Mapped[datetime | None] = mapped_column(default=None)

    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)


class SyncTask(IdMixin, Base):
    """One rclone transfer."""

    __tablename__ = "sync_tasks"
    __table_args__ = (
        Index("ix_sync_tasks_status_created", "status", "created_at"),
        enum_check("direction", SyncDirection, constraint="direction_valid"),
        enum_check("status", SyncStatus, constraint="status_valid"),
    )

    backup_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey,
        ForeignKey("backup_history.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    direction: Mapped[str] = mapped_column(String(16), default=SyncDirection.UPLOAD.value)
    status: Mapped[str] = mapped_column(String(16), default=SyncStatus.QUEUED.value)
    agent_task_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    remote_name: Mapped[str | None] = mapped_column(String(64), default=None)
    remote_path: Mapped[str | None] = mapped_column(String(512), default=None)

    bytes_total: Mapped[int | None] = mapped_column(BigInteger, default=None)
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    transfer_rate_bps: Mapped[int | None] = mapped_column(BigInteger, default=None)

    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)

    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RestoreHistory(IdMixin, Base):
    """A restore request and its outcome.

    A row exists from the moment a restore is *requested*, in state
    `pending_confirmation` — the two-phase flow means a restore is recorded before it is
    permitted to run, never after.
    """

    __tablename__ = "restore_history"
    __table_args__ = (
        Index("ix_restore_history_status_created", "status", "created_at"),
        enum_check("source", RestoreSource, constraint="source_valid"),
        enum_check("target_type", GuestType, constraint="target_type_valid"),
        enum_check("restore_mode", RestoreMode, constraint="restore_mode_valid"),
        enum_check("status", RestoreStatus, constraint="status_valid"),
    )

    backup_id: Mapped[int] = mapped_column(
        BigIntForeignKey, ForeignKey("backup_history.id", ondelete="RESTRICT"), index=True
    )
    source: Mapped[str] = mapped_column(String(16), default=RestoreSource.LOCAL.value)

    target_vmid: Mapped[int] = mapped_column(Integer)
    target_type: Mapped[str] = mapped_column(String(8))
    restore_mode: Mapped[str] = mapped_column(String(16))
    target_node: Mapped[str] = mapped_column(String(64))
    target_storage: Mapped[str] = mapped_column(String(64))

    overwrite_existing: Mapped[bool] = mapped_column(Boolean, default=False)
    force_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    start_after_restore: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(
        String(32), default=RestoreStatus.PENDING_CONFIRMATION.value
    )
    confirmation_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    preflight: Mapped[JsonDict | None] = mapped_column(default=None)

    agent_task_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    requested_by: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    log_path: Mapped[str | None] = mapped_column(String(512), default=None)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RetentionEvent(IdMixin, Base):
    """Every retention decision, kept or deleted, with the policy that produced it.

    Auditable by construction: "why is this backup gone?" must always have an answer.
    """

    __tablename__ = "retention_events"
    __table_args__ = (
        Index("ix_retention_events_guest_created", "vmid", "guest_type", "created_at"),
        enum_check("guest_type", GuestType, constraint="guest_type_valid"),
        enum_check("action", RetentionAction, constraint="action_valid"),
    )

    backup_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("backup_history.id", ondelete="SET NULL"), default=None
    )
    vmid: Mapped[int] = mapped_column(Integer)
    guest_type: Mapped[str] = mapped_column(String(8))

    action: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(255))
    policy_snapshot: Mapped[JsonDict | None] = mapped_column(default=None)
    freed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
