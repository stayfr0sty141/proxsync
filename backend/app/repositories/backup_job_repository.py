"""Backup schedule persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models.backup import BackupJob, BackupJobTarget
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import GuestType


class BackupJobRepository(Protocol):
    async def get(self, job_id: int) -> BackupJob | None: ...
    async def get_by_name(self, name: str) -> BackupJob | None: ...
    async def list_all(self) -> list[BackupJob]: ...
    async def list_enabled(self) -> list[BackupJob]: ...
    async def count(self) -> int: ...
    async def add(self, job: BackupJob) -> BackupJob: ...
    async def delete(self, job: BackupJob) -> None: ...
    async def replace_targets(
        self, job: BackupJob, targets: list[tuple[int, GuestType]]
    ) -> None: ...
    async def set_schedule_state(
        self, job: BackupJob, *, last_run_at: datetime | None, next_run_at: datetime | None
    ) -> None: ...


class SqlAlchemyBackupJobRepository(SqlAlchemyRepository):
    async def get(self, job_id: int) -> BackupJob | None:
        statement = (
            select(BackupJob).where(BackupJob.id == job_id).options(selectinload(BackupJob.targets))
        )
        return (await self._session.execute(statement)).scalars().first()

    async def get_by_name(self, name: str) -> BackupJob | None:
        statement = (
            select(BackupJob)
            .where(func.lower(BackupJob.name) == name.strip().lower())
            .options(selectinload(BackupJob.targets))
        )
        return (await self._session.execute(statement)).scalars().first()

    async def list_all(self) -> list[BackupJob]:
        statement = (
            select(BackupJob).order_by(BackupJob.name).options(selectinload(BackupJob.targets))
        )
        return list((await self._session.execute(statement)).scalars())

    async def list_enabled(self) -> list[BackupJob]:
        statement = (
            select(BackupJob)
            .where(BackupJob.enabled.is_(True))
            .order_by(BackupJob.id)
            .options(selectinload(BackupJob.targets))
        )
        return list((await self._session.execute(statement)).scalars())

    async def count(self) -> int:
        statement = select(func.count()).select_from(BackupJob)
        return int((await self._session.execute(statement)).scalar_one())

    async def add(self, job: BackupJob) -> BackupJob:
        self._session.add(job)
        await self._session.flush()
        return job

    async def delete(self, job: BackupJob) -> None:
        await self._session.delete(job)
        await self._session.flush()

    async def replace_targets(self, job: BackupJob, targets: list[tuple[int, GuestType]]) -> None:
        """Replace the target list wholesale.

        `BackupJobUpdate` is a full replacement, so a diff would add complexity without
        changing the outcome; `delete-orphan` on the relationship removes the old rows.
        """
        job.targets.clear()
        for vmid, guest_type in targets:
            job.targets.append(BackupJobTarget(vmid=vmid, guest_type=guest_type.value))
        await self._session.flush()

    async def set_schedule_state(
        self, job: BackupJob, *, last_run_at: datetime | None, next_run_at: datetime | None
    ) -> None:
        if last_run_at is not None:
            job.last_run_at = last_run_at
        job.next_run_at = next_run_at
        await self._session.flush()
