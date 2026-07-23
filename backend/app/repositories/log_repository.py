"""Log persistence and the query grammar behind `/logs`.

Reads are keyset-paginated for export and offset-paginated for the UI. That split is
deliberate: a page of 50 rows is what an offset is good at, and a 90-day export is what it is
worst at — `OFFSET 400000` re-walks the whole index on both engines.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, Select, delete, func, select

from app.db.models.system import LogEntry
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import LogCategory, LogLevel

EXPORT_CHUNK_SIZE = 500


@dataclass(frozen=True, slots=True)
class NewLogEntry:
    """One captured structlog event, already flattened by the sink."""

    ts: datetime
    level: LogLevel
    category: LogCategory
    message: str
    context: dict[str, Any] | None = None
    correlation_id: str | None = None
    user_id: int | None = None
    backup_id: int | None = None
    restore_id: int | None = None
    sync_task_id: int | None = None


class LogRepository(Protocol):
    async def add_many(self, entries: Sequence[NewLogEntry]) -> int: ...
    async def search(
        self,
        *,
        category: LogCategory | None = None,
        level: LogLevel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        backup_id: int | None = None,
        restore_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LogEntry]: ...
    async def count(self, **filters: Any) -> int: ...
    def iterate(
        self, *, chunk_size: int = EXPORT_CHUNK_SIZE, **filters: Any
    ) -> AsyncIterator[list[LogEntry]]: ...
    async def prune(self, *, before: datetime) -> int: ...


class SqlAlchemyLogRepository(SqlAlchemyRepository):
    async def add_many(self, entries: Sequence[NewLogEntry]) -> int:
        if not entries:
            return 0
        self._session.add_all(
            [
                LogEntry(
                    ts=entry.ts,
                    level=entry.level.value,
                    category=entry.category.value,
                    message=entry.message,
                    context=entry.context,
                    correlation_id=entry.correlation_id,
                    user_id=entry.user_id,
                    backup_id=entry.backup_id,
                    restore_id=entry.restore_id,
                    sync_task_id=entry.sync_task_id,
                )
                for entry in entries
            ]
        )
        await self._session.flush()
        return len(entries)

    def _filtered(
        self,
        *,
        category: LogCategory | None = None,
        level: LogLevel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        backup_id: int | None = None,
        restore_id: int | None = None,
    ) -> Select[tuple[LogEntry]]:
        statement = select(LogEntry)
        if category is not None:
            statement = statement.where(LogEntry.category == category.value)
        if level is not None:
            statement = statement.where(LogEntry.level == level.value)
        if since is not None:
            statement = statement.where(LogEntry.ts >= since)
        if until is not None:
            statement = statement.where(LogEntry.ts <= until)
        if correlation_id:
            statement = statement.where(LogEntry.correlation_id == correlation_id)
        if backup_id is not None:
            statement = statement.where(LogEntry.backup_id == backup_id)
        if restore_id is not None:
            statement = statement.where(LogEntry.restore_id == restore_id)
        if query:
            # `message` only. The context column is JSON on SQLite and JSONB on PostgreSQL, and
            # a LIKE over serialised JSON matches key names as readily as values — a search for
            # "error" would return every row that merely has an error field.
            statement = statement.where(LogEntry.message.ilike(f"%{query}%"))
        return statement

    async def search(
        self,
        *,
        category: LogCategory | None = None,
        level: LogLevel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        backup_id: int | None = None,
        restore_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LogEntry]:
        statement = (
            self._filtered(
                category=category,
                level=level,
                since=since,
                until=until,
                query=query,
                correlation_id=correlation_id,
                backup_id=backup_id,
                restore_id=restore_id,
            )
            .order_by(LogEntry.ts.desc(), LogEntry.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(statement)).scalars())

    async def count(self, **filters: Any) -> int:
        statement = select(func.count()).select_from(self._filtered(**filters).subquery())
        return int((await self._session.execute(statement)).scalar_one())

    async def iterate(
        self, *, chunk_size: int = EXPORT_CHUNK_SIZE, **filters: Any
    ) -> AsyncIterator[list[LogEntry]]:
        """Walk the whole filtered set newest-first, one chunk at a time.

        Keyset, not offset: an export runs while the sink is still writing, and an offset would
        skip or repeat rows as the set shifts underneath it.
        """
        cursor: int | None = None
        while True:
            statement = self._filtered(**filters)
            if cursor is not None:
                statement = statement.where(LogEntry.id < cursor)
            statement = statement.order_by(LogEntry.id.desc()).limit(chunk_size)
            rows = list((await self._session.execute(statement)).scalars())
            if not rows:
                return
            yield rows
            cursor = rows[-1].id
            if len(rows) < chunk_size:
                return

    async def prune(self, *, before: datetime) -> int:
        result = await self._session.execute(delete(LogEntry).where(LogEntry.ts < before))
        return int(cast("CursorResult[Any]", result).rowcount or 0)
