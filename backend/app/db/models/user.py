"""Users and refresh-token families."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, enum_check
from app.db.types import BigIntForeignKey
from app.schemas.enums import UserRole


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (enum_check("role", UserRole, constraint="role_valid"),)

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """Stored case-folded; the service lowercases on write and on lookup."""
    email: Mapped[str | None] = mapped_column(String(255), unique=True, default=None)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.VIEWER.value)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    last_login_ip: Mapped[str | None] = mapped_column(String(45), default=None)

    @property
    def role_enum(self) -> UserRole:
        return UserRole(self.role)


class RefreshToken(IdMixin, Base):
    """One row per issued refresh token.

    Tokens are stored as SHA-256 digests only. `family_id` groups every token descended from
    a single login: presenting a token that has already been rotated revokes the whole family,
    which is how theft is detected.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_user_expiry", "user_id", "expires_at"),)

    user_id: Mapped[int] = mapped_column(
        BigIntForeignKey, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)

    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), default=None)

    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(45), default=None)

    created_at: Mapped[datetime]

    def is_usable(self, *, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
