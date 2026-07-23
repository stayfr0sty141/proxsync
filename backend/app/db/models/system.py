"""Settings, logs, notifications, storage samples and the audit trail."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, enum_check, utcnow
from app.db.types import BigIntForeignKey, JsonDict
from app.schemas.enums import (
    AuditAction,
    AuditResult,
    LogCategory,
    LogLevel,
    NotificationChannel,
    NotificationEvent,
    NotificationStatus,
    SettingsSection,
    SettingValueType,
)


class Setting(IdMixin, Base):
    """Typed key–value store, one row per setting.

    `is_secret` rows hold a Fernet token in `value`, never plaintext, and the API returns a
    masked hint instead of the value.
    """

    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("section", "key", name="uq_settings_section_key"),
        enum_check("section", SettingsSection, constraint="section_valid"),
        enum_check("value_type", SettingValueType, constraint="value_type_valid"),
    )

    section: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[JsonDict | None] = mapped_column(default=None)
    """Always wrapped as ``{"v": <scalar-or-object>}`` so every dialect stores valid JSON."""
    value_type: Mapped[str] = mapped_column(String(16), default=SettingValueType.STRING.value)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)

    description: Mapped[str | None] = mapped_column(String(255), default=None)
    updated_by: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class LogEntry(IdMixin, Base):
    __tablename__ = "logs"
    __table_args__ = (
        Index("ix_logs_category_ts", "category", "ts"),
        Index("ix_logs_level_ts", "level", "ts"),
        enum_check("level", LogLevel, constraint="level_valid"),
        enum_check("category", LogCategory, constraint="category_valid"),
    )

    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[JsonDict | None] = mapped_column(default=None)

    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    user_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    backup_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("backup_history.id", ondelete="SET NULL"), default=None
    )
    restore_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("restore_history.id", ondelete="SET NULL"), default=None
    )
    sync_task_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("sync_tasks.id", ondelete="SET NULL"), default=None
    )


class Notification(IdMixin, Base):
    """Outbox row.

    Written in the same transaction as the state change it describes, then delivered by a
    worker — so a Telegram outage delays notifications but never loses one, and never blocks
    a backup.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_status_created", "status", "created_at"),
        Index("ix_notifications_dedupe", "channel", "event_type", "dedupe_key", "created_at"),
        enum_check("channel", NotificationChannel, constraint="channel_valid"),
        enum_check("event_type", NotificationEvent, constraint="event_type_valid"),
        enum_check("status", NotificationStatus, constraint="status_valid"),
    )

    channel: Mapped[str] = mapped_column(String(16), default=NotificationChannel.TELEGRAM.value)
    event_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=NotificationStatus.PENDING.value)
    payload: Mapped[JsonDict] = mapped_column()

    dedupe_key: Mapped[str | None] = mapped_column(String(128), default=None)
    """Identity of the *thing being reported*, not of this row.

    Two notifications sharing one key describe the same occurrence, which is what lets the
    outbox tell a repeated report from a new one. Not unique: a suppressed row deliberately
    carries the key of the row that displaced it.
    """

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    backup_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("backup_history.id", ondelete="SET NULL"), default=None
    )
    restore_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("restore_history.id", ondelete="SET NULL"), default=None
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class StorageSnapshot(IdMixin, Base):
    """Periodic usage sample. Feeds the storage trend chart and the fill-date forecast."""

    __tablename__ = "storage_snapshots"

    captured_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    local_total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    local_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    local_free_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    remote_used_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    remote_quota_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    backup_count: Mapped[int] = mapped_column(Integer, default=0)
    oldest_backup_at: Mapped[datetime | None] = mapped_column(default=None)


class AuditEvent(IdMixin, Base):
    """Security-relevant actions.

    Kept separate from `logs` because its retention and immutability requirements differ:
    application logs are pruned aggressively, audit rows are not.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_action_ts", "action", "ts"),
        enum_check("action", AuditAction, constraint="action_valid"),
        enum_check("result", AuditResult, constraint="result_valid"),
    )

    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    username_snapshot: Mapped[str | None] = mapped_column(String(64), default=None)
    """Recorded verbatim so the trail survives the user being renamed or deleted."""

    action: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(16), default=AuditResult.SUCCESS.value)
    resource_type: Mapped[str | None] = mapped_column(String(32), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(64), default=None)

    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    detail: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
