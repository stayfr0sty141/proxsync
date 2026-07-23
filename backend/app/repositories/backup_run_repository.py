"""Backup run persistence.

A *run* is one execution: one schedule firing, or one press of **Backup Now**. It groups the
per-guest `backup_history` rows so the UI can say "3 of 5 guests done" instead of showing
five unrelated entries.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select

from app.db.models.backup import BackupRun
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import RunStatus, TriggerType

ACTIVE_RUN_STATES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)


class BackupRunRepository(Protocol):
    async def get(self, run_id: int) -> BackupRun | None: ...
    async def create(
        self,
        *,
        job_id: int | None,
        trigger: TriggerType,
        requested_by: int | None,
        targets: list[dict[str, Any]],
        correlation_id: str,
    ) -> BackupRun: ...
    async def list_recent(
        self, *, limit: int = 50, offset: int = 0, status: RunStatus | None = None
    ) -> list[BackupRun]: ...
    async def count(self, *, status: RunStatus | None = None) -> int: ...
    async def active(self) -> list[BackupRun]: ...
    async def active_for_job(self, job_id: int) -> BackupRun | None: ...
    async def next_queued(self) -> BackupRun | None: ...


class SqlAlchemyBackupRunRepository(SqlAlchemyRepository):
    async def get(self, run_id: int) -> BackupRun | None:
        return await self._session.get(BackupRun, run_id)

    async def create(
        self,
        *,
        job_id: int | None,
        trigger: TriggerType,
        requested_by: int | None,
        targets: list[dict[str, Any]],
        correlation_id: str,
    ) -> BackupRun:
        run = BackupRun(
            job_id=job_id,
            trigger=trigger.value,
            requested_by=requested_by,
            status=RunStatus.QUEUED.value,
            guest_total=len(targets),
            targets=targets,
            correlation_id=correlation_id,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def list_recent(
        self, *, limit: int = 50, offset: int = 0, status: RunStatus | None = None
    ) -> list[BackupRun]:
        statement = select(BackupRun).order_by(BackupRun.created_at.desc(), BackupRun.id.desc())
        if status is not None:
            statement = statement.where(BackupRun.status == status.value)
        statement = statement.limit(limit).offset(offset)
        return list((await self._session.execute(statement)).scalars())

    async def count(self, *, status: RunStatus | None = None) -> int:
        statement = select(func.count()).select_from(BackupRun)
        if status is not None:
            statement = statement.where(BackupRun.status == status.value)
        return int((await self._session.execute(statement)).scalar_one())

    async def active(self) -> list[BackupRun]:
        """Runs that were queued or in flight — the set a restart has to reconcile."""
        statement = (
            select(BackupRun).where(BackupRun.status.in_(ACTIVE_RUN_STATES)).order_by(BackupRun.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def next_queued(self) -> BackupRun | None:
        """The oldest queued run — the head of the queue.

        Ordered ascending, unlike every read-side listing on this class. The UI wants the
        newest run first; a queue that served the newest first would be a stack, and a run
        submitted during a backlog could be starved indefinitely.
        """
        statement = (
            select(BackupRun)
            .where(BackupRun.status == RunStatus.QUEUED.value)
            .order_by(BackupRun.id)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def active_for_job(self, job_id: int) -> BackupRun | None:
        """Guards against a slow weekly backup being started again by the next firing."""
        statement = (
            select(BackupRun)
            .where(BackupRun.job_id == job_id, BackupRun.status.in_(ACTIVE_RUN_STATES))
            .order_by(BackupRun.id)
        )
        return (await self._session.execute(statement)).scalars().first()
