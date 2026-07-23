"""Declarative base, naming convention and shared mixins.

The naming convention is not cosmetic: Alembic's SQLite batch mode rebuilds tables to emulate
`ALTER`, and it can only reproduce constraints that have names. Without this, every future
migration touching a constrained column would silently drop the constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import (
    BigIntPrimaryKey,
    JsonDict,
    JsonList,
    JSONVariant,
    UTCDateTime,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this as a class attribute
        datetime: UTCDateTime,
        str: String(255),
        JsonDict: JSONVariant,
        JsonList: JSONVariant,
    }

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class IdMixin:
    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True, autoincrement=True)


class TimestampMixin:
    """Application-side timestamps.

    Deliberately not `server_default=func.now()`: SQLite's CURRENT_TIMESTAMP has second
    granularity and no timezone, so two engines would disagree. Every write goes through the
    ORM, so Python-side defaults are authoritative and identical everywhere.
    """

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


def enum_check(column: str, enum_cls: type[StrEnum], *, constraint: str) -> CheckConstraint:
    """CHECK constraint mirroring a Python StrEnum.

    Values are rendered with `repr`, which produces SQL-safe single-quoted literals for the
    identifier-like strings these enums contain.
    """
    values = ", ".join(repr(member.value) for member in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=constraint)
