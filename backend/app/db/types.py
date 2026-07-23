"""Portable column types.

SQLite has no timezone-aware timestamp and no JSON type; PostgreSQL has both. These
decorators make the application see the same thing either way, which is the whole point of
keeping a PostgreSQL migration mechanical.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Dialect, Integer, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB

# SQLite only treats a column as the rowid alias — and therefore only autoincrements it —
# when its declared type is exactly INTEGER. A `BIGINT PRIMARY KEY` silently loses
# autoincrement, and every insert then fails with "NOT NULL constraint failed". The variant
# keeps BIGINT on PostgreSQL, where 2^31 rows of backup history is a real ceiling, and uses
# INTEGER on SQLite, where the rowid is a 64-bit integer regardless of the declared type.
BigIntPrimaryKey = BigInteger().with_variant(Integer, "sqlite")
BigIntForeignKey = BigInteger().with_variant(Integer, "sqlite")


class UTCDateTime(TypeDecorator[datetime]):
    """Stores tz-aware datetimes as UTC and returns them tz-aware.

    Naive datetimes are rejected on write rather than silently assumed to be UTC — a wrong
    assumption here shows up months later as a backup timestamped an hour off.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:  # noqa: ARG002 - required by the TypeDecorator interface
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "Naive datetime passed to a UTCDateTime column; "
                "use app.db.base.utcnow() or attach a timezone explicitly"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:  # noqa: ARG002 - required by the TypeDecorator interface
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# JSONB on PostgreSQL (indexable, binary), plain JSON text on SQLite.
JSONVariant = JSON().with_variant(JSONB(), "postgresql")

JsonDict = dict[str, Any]
JsonList = list[Any]
