"""Storage-monitor persistence and read-side aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import case, func, select

from app.db.models.backup import BackupHistory
from app.db.models.system import StorageSnapshot
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import BackupStatus


@dataclass(frozen=True, slots=True)
class StorageSnapshotRecord:
    id: int
    captured_at: datetime
    local_total_bytes: int
    local_used_bytes: int
    local_free_bytes: int
    remote_used_bytes: int | None
    remote_quota_bytes: int | None
    backup_count: int
    oldest_backup_at: datetime | None


@dataclass(frozen=True, slots=True)
class NewStorageSnapshot:
    captured_at: datetime
    local_total_bytes: int
    local_used_bytes: int
    local_free_bytes: int
    remote_used_bytes: int | None
    remote_quota_bytes: int | None
    backup_count: int
    oldest_backup_at: datetime | None


@dataclass(frozen=True, slots=True)
class GuestStorageRecord:
    vmid: int
    guest_type: str
    guest_name: str
    backup_count: int
    total_bytes: int
    latest_backup_at: datetime | None


class StorageSnapshotRepository(Protocol):
    async def create(self, sample: NewStorageSnapshot) -> StorageSnapshotRecord: ...
    async def latest(self) -> StorageSnapshotRecord | None: ...
    async def since(
        self, *, cutoff: datetime, until: datetime | None = None
    ) -> list[StorageSnapshotRecord]: ...
    async def oldest_local_backup_at(self) -> datetime | None: ...
    async def by_guest(self) -> list[GuestStorageRecord]: ...


class SqlAlchemyStorageSnapshotRepository(SqlAlchemyRepository):
    async def create(self, sample: NewStorageSnapshot) -> StorageSnapshotRecord:
        row = StorageSnapshot(
            captured_at=sample.captured_at,
            local_total_bytes=sample.local_total_bytes,
            local_used_bytes=sample.local_used_bytes,
            local_free_bytes=sample.local_free_bytes,
            remote_used_bytes=sample.remote_used_bytes,
            remote_quota_bytes=sample.remote_quota_bytes,
            backup_count=sample.backup_count,
            oldest_backup_at=sample.oldest_backup_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _snapshot_record(row)

    async def latest(self) -> StorageSnapshotRecord | None:
        statement = (
            select(StorageSnapshot)
            .order_by(StorageSnapshot.captured_at.desc(), StorageSnapshot.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(statement)).scalars().first()
        return _snapshot_record(row) if row is not None else None

    async def since(
        self, *, cutoff: datetime, until: datetime | None = None
    ) -> list[StorageSnapshotRecord]:
        statement = select(StorageSnapshot).where(StorageSnapshot.captured_at >= cutoff)
        if until is not None:
            statement = statement.where(StorageSnapshot.captured_at <= until)
        statement = statement.order_by(StorageSnapshot.captured_at, StorageSnapshot.id)
        return [_snapshot_record(row) for row in (await self._session.execute(statement)).scalars()]

    async def oldest_local_backup_at(self) -> datetime | None:
        statement = select(func.min(BackupHistory.started_at)).where(
            BackupHistory.status == BackupStatus.SUCCESS.value,
            BackupHistory.local_deleted_at.is_(None),
            BackupHistory.filename.is_not(None),
        )
        return (await self._session.execute(statement)).scalar_one()

    async def by_guest(self) -> list[GuestStorageRecord]:
        """Managed local bytes, grouped by the stable guest identity.

        The window runs before aggregation so the displayed name comes from exactly the newest
        eligible backup. Grouping by ``guest_name`` would split one guest after it was renamed;
        using ``max(guest_name)`` would choose a lexical accident rather than the latest name.
        """

        rank = func.row_number().over(
            partition_by=(BackupHistory.vmid, BackupHistory.guest_type),
            order_by=(BackupHistory.started_at.desc(), BackupHistory.id.desc()),
        )
        eligible = (
            select(
                BackupHistory.vmid.label("vmid"),
                BackupHistory.guest_type.label("guest_type"),
                BackupHistory.guest_name.label("guest_name"),
                BackupHistory.size_bytes.label("size_bytes"),
                BackupHistory.started_at.label("started_at"),
                rank.label("newest_rank"),
            )
            .where(
                BackupHistory.status == BackupStatus.SUCCESS.value,
                BackupHistory.local_deleted_at.is_(None),
                BackupHistory.filename.is_not(None),
                BackupHistory.size_bytes.is_not(None),
            )
            .subquery()
        )
        newest_name = func.max(case((eligible.c.newest_rank == 1, eligible.c.guest_name)))
        total_bytes = func.sum(eligible.c.size_bytes)
        statement = (
            select(
                eligible.c.vmid,
                eligible.c.guest_type,
                newest_name.label("guest_name"),
                func.count().label("backup_count"),
                total_bytes.label("total_bytes"),
                func.max(eligible.c.started_at).label("latest_backup_at"),
            )
            .group_by(eligible.c.vmid, eligible.c.guest_type)
            .order_by(total_bytes.desc(), eligible.c.vmid, eligible.c.guest_type)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            GuestStorageRecord(
                vmid=int(row.vmid),
                guest_type=str(row.guest_type),
                guest_name=str(row.guest_name),
                backup_count=int(row.backup_count),
                total_bytes=int(row.total_bytes),
                latest_backup_at=row.latest_backup_at,
            )
            for row in rows
        ]


def _snapshot_record(row: StorageSnapshot) -> StorageSnapshotRecord:
    return StorageSnapshotRecord(
        id=row.id,
        captured_at=row.captured_at,
        local_total_bytes=row.local_total_bytes,
        local_used_bytes=row.local_used_bytes,
        local_free_bytes=row.local_free_bytes,
        remote_used_bytes=row.remote_used_bytes,
        remote_quota_bytes=row.remote_quota_bytes,
        backup_count=row.backup_count,
        oldest_backup_at=row.oldest_backup_at,
    )
