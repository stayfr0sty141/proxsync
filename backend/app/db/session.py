"""Engine, session factory and SQLite pragmas.

Pragmas are applied on every physical connection, not once at startup: SQLite settings are
per-connection, and a pool that opens a second connection without them would silently lose
foreign-key enforcement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from app.core.config import Settings
from app.core.logging import logger

SQLITE_PRAGMAS = {
    "foreign_keys": "ON",
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "busy_timeout": "5000",
}


def _register_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cursor = dbapi_connection.cursor()
        try:
            for pragma, value in SQLITE_PRAGMAS.items():
                cursor.execute(f"PRAGMA {pragma}={value}")
        finally:
            cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": settings.database_echo, "future": True}

    if settings.is_sqlite:
        sqlite_path = settings.sqlite_path
        if sqlite_path is not None:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        kwargs["pool_size"] = settings.database_pool_size
        kwargs["max_overflow"] = settings.database_max_overflow
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(settings.database_url, **kwargs)
    if settings.is_sqlite:
        _register_sqlite_pragmas(engine)
    return engine


class Database:
    """Owns the engine and hands out sessions. One instance per application."""

    def __init__(self, settings: Settings) -> None:
        self._engine = create_engine(settings)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope: commit on success, roll back on any exception."""
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def check(self) -> bool:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - health checks report, they do not raise
            logger.error("database_check_failed", exc_info=True)
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()


def resolve_sqlite_directory(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None
    _, _, tail = database_url.partition("///")
    return Path("/" + tail.lstrip("/")).parent if tail else None
