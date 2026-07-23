"""Repository foundations.

Services depend on the `Protocol` definitions in this package, never on SQLAlchemy. That is
what lets the auth and settings services be unit-tested without a database, and what keeps a
PostgreSQL migration mechanical: the query layer is the only thing that knows the dialect
exists.
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class SqlAlchemyRepository:
    """Base for SQLAlchemy-backed repositories.

    Repositories never commit. The unit of work is the request (or the background task), and
    its boundary is owned by `Database.session()` — a repository that committed on its own
    would break atomicity between, say, revoking a token family and writing the audit row.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def flush(self) -> None:
        """Force pending INSERTs so generated primary keys become available."""
        await self._session.flush()
