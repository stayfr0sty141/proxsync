"""Transfer queue persistence.

`sync_tasks` is a durable work queue, not a log: `attempt`, `max_attempts` and `next_retry_at`
are the retry state itself, so a dashboard restart resumes the backoff schedule rather than
starting it again from zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import func, or_, select

from app.db.models.backup import SyncTask
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import SyncDirection, SyncStatus

ACTIVE_SYNC_STATES = (SyncStatus.QUEUED.value, SyncStatus.RUNNING.value)


class SyncTaskRepository(Protocol):
    async def get(self, task_id: int) -> SyncTask | None: ...
    async def create(
        self,
        *,
        backup_id: int | None,
        direction: SyncDirection,
        remote_name: str,
        remote_path: str,
        max_attempts: int,
        correlation_id: str | None,
    ) -> SyncTask: ...
    async def next_ready(self, *, now: datetime) -> SyncTask | None: ...
    async def active_for_backup(self, backup_id: int) -> SyncTask | None: ...
    async def running(self) -> list[SyncTask]: ...
    async def list_recent(
        self, *, limit: int = 50, offset: int = 0, status: SyncStatus | None = None
    ) -> list[SyncTask]: ...
    async def count(self, *, status: SyncStatus | None = None) -> int: ...


class SqlAlchemySyncTaskRepository(SqlAlchemyRepository):
    async def get(self, task_id: int) -> SyncTask | None:
        return await self._session.get(SyncTask, task_id)

    async def create(
        self,
        *,
        backup_id: int | None,
        direction: SyncDirection,
        remote_name: str,
        remote_path: str,
        max_attempts: int,
        correlation_id: str | None,
    ) -> SyncTask:
        task = SyncTask(
            backup_id=backup_id,
            direction=direction.value,
            status=SyncStatus.QUEUED.value,
            remote_name=remote_name,
            remote_path=remote_path,
            max_attempts=max_attempts,
            correlation_id=correlation_id,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def next_ready(self, *, now: datetime) -> SyncTask | None:
        """The oldest queued transfer whose backoff has elapsed.

        Ascending by id, unlike the read-side listings: a queue that served the newest first
        would starve the transfer that has already failed twice and waited longest.
        """
        statement = (
            select(SyncTask)
            .where(
                SyncTask.status == SyncStatus.QUEUED.value,
                or_(SyncTask.next_retry_at.is_(None), SyncTask.next_retry_at <= now),
            )
            .order_by(SyncTask.id)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def active_for_backup(self, backup_id: int) -> SyncTask | None:
        """Guards against queueing the same artifact twice."""
        statement = (
            select(SyncTask)
            .where(
                SyncTask.backup_id == backup_id,
                SyncTask.status.in_(ACTIVE_SYNC_STATES),
            )
            .order_by(SyncTask.id)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def running(self) -> list[SyncTask]:
        """Transfers a restart has to reconcile."""
        statement = (
            select(SyncTask)
            .where(SyncTask.status == SyncStatus.RUNNING.value)
            .order_by(SyncTask.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def list_recent(
        self, *, limit: int = 50, offset: int = 0, status: SyncStatus | None = None
    ) -> list[SyncTask]:
        statement = select(SyncTask).order_by(SyncTask.created_at.desc(), SyncTask.id.desc())
        if status is not None:
            statement = statement.where(SyncTask.status == status.value)
        return list((await self._session.execute(statement.limit(limit).offset(offset))).scalars())

    async def count(self, *, status: SyncStatus | None = None) -> int:
        statement = select(func.count()).select_from(SyncTask)
        if status is not None:
            statement = statement.where(SyncTask.status == status.value)
        return int((await self._session.execute(statement)).scalar_one())
