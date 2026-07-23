"""Cached Proxmox guest inventory.

Rows are never deleted when a guest disappears from the host: backup history joins against
them, and losing the name would make old backups unreadable in the UI. `last_seen_at` is how
the dashboard distinguishes a live guest from a historical one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, enum_check, utcnow
from app.db.types import JsonDict, JsonList
from app.schemas.enums import GuestStatus, GuestType


class Guest(IdMixin, Base):
    __tablename__ = "guests"
    __table_args__ = (
        UniqueConstraint("node", "vmid", name="uq_guests_node_vmid"),
        Index("ix_guests_type_status", "guest_type", "status"),
        enum_check("guest_type", GuestType, constraint="guest_type_valid"),
        enum_check("status", GuestStatus, constraint="status_valid"),
    )

    vmid: Mapped[int] = mapped_column(Integer, index=True)
    guest_type: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(128))
    node: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=GuestStatus.UNKNOWN.value)

    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    """The allow-list. A guest must be enabled here before any job may target it."""

    tags: Mapped[JsonList | None] = mapped_column(default=None)
    raw: Mapped[JsonDict | None] = mapped_column(default=None)
    """Last payload from the Proxmox API, kept for diagnostics."""

    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
