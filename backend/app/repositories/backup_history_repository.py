"""Backup artifact persistence — the central table of the application."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import ColumnElement, Select, func, or_, select

from app.db.models.backup import BackupHistory
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import BackupMode, BackupStatus, Compression, GuestType, UploadStatus


class BackupHistoryRepository(Protocol):
    async def get(self, backup_id: int) -> BackupHistory | None: ...
    async def create(
        self,
        *,
        run_id: int | None,
        guest_id: int | None,
        vmid: int,
        guest_type: GuestType,
        guest_name: str,
        node: str,
        storage: str,
        mode: BackupMode,
        compression: Compression,
        upload_status: UploadStatus,
        correlation_id: str | None,
        started_at: datetime,
    ) -> BackupHistory: ...
    async def search(
        self,
        *,
        vmid: int | None = None,
        guest_type: GuestType | None = None,
        status: BackupStatus | None = None,
        upload_status: UploadStatus | None = None,
        run_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BackupHistory]: ...
    async def count(
        self,
        *,
        vmid: int | None = None,
        guest_type: GuestType | None = None,
        status: BackupStatus | None = None,
        upload_status: UploadStatus | None = None,
        run_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        include_deleted: bool = False,
    ) -> int: ...
    async def for_run(self, run_id: int) -> list[BackupHistory]: ...
    async def in_flight(self) -> list[BackupHistory]: ...
    async def last_successful(self) -> BackupHistory | None: ...


class SqlAlchemyBackupHistoryRepository(SqlAlchemyRepository):
    async def get(self, backup_id: int) -> BackupHistory | None:
        return await self._session.get(BackupHistory, backup_id)

    async def create(
        self,
        *,
        run_id: int | None,
        guest_id: int | None,
        vmid: int,
        guest_type: GuestType,
        guest_name: str,
        node: str,
        storage: str,
        mode: BackupMode,
        compression: Compression,
        upload_status: UploadStatus,
        correlation_id: str | None,
        started_at: datetime,
    ) -> BackupHistory:
        record = BackupHistory(
            run_id=run_id,
            guest_id=guest_id,
            vmid=vmid,
            guest_type=guest_type.value,
            guest_name=guest_name,
            node=node,
            storage=storage,
            mode=mode.value,
            compression=compression.value,
            status=BackupStatus.RUNNING.value,
            upload_status=upload_status.value,
            correlation_id=correlation_id,
            started_at=started_at,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def search(
        self,
        *,
        vmid: int | None = None,
        guest_type: GuestType | None = None,
        status: BackupStatus | None = None,
        upload_status: UploadStatus | None = None,
        run_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        include_deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BackupHistory]:
        statement = self._filtered(
            vmid=vmid,
            guest_type=guest_type,
            status=status,
            upload_status=upload_status,
            run_id=run_id,
            since=since,
            until=until,
            query=query,
            include_deleted=include_deleted,
            base=select(BackupHistory),
        )
        statement = (
            statement.order_by(BackupHistory.started_at.desc(), BackupHistory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(statement)).scalars())

    async def count(
        self,
        *,
        vmid: int | None = None,
        guest_type: GuestType | None = None,
        status: BackupStatus | None = None,
        upload_status: UploadStatus | None = None,
        run_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        include_deleted: bool = False,
    ) -> int:
        statement = self._filtered(
            vmid=vmid,
            guest_type=guest_type,
            status=status,
            upload_status=upload_status,
            run_id=run_id,
            since=since,
            until=until,
            query=query,
            include_deleted=include_deleted,
            base=select(func.count()).select_from(BackupHistory),
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def for_run(self, run_id: int) -> list[BackupHistory]:
        statement = (
            select(BackupHistory).where(BackupHistory.run_id == run_id).order_by(BackupHistory.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def in_flight(self) -> list[BackupHistory]:
        """Rows the agent may still be working on — what a restart has to re-adopt."""
        statement = (
            select(BackupHistory)
            .where(
                BackupHistory.status == BackupStatus.RUNNING.value,
                BackupHistory.agent_task_id.is_not(None),
            )
            .order_by(BackupHistory.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def awaiting_upload(self, *, limit: int = 100) -> list[BackupHistory]:
        """Successful backups whose artifact still has to reach the remote.

        Oldest first: an upload waiting since last week matters more than one that finished a
        minute ago. `status == success` is part of the filter rather than an assumption — a
        failed backup has no artifact worth copying — and a locally deleted one is skipped
        because there is nothing left to send.
        """
        statement = (
            select(BackupHistory)
            .where(
                BackupHistory.status == BackupStatus.SUCCESS.value,
                BackupHistory.upload_status == UploadStatus.PENDING.value,
                BackupHistory.filename.is_not(None),
                BackupHistory.local_deleted_at.is_(None),
            )
            .order_by(BackupHistory.id)
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())

    async def count_awaiting_upload(self) -> int:
        statement = (
            select(func.count())
            .select_from(BackupHistory)
            .where(
                BackupHistory.status == BackupStatus.SUCCESS.value,
                BackupHistory.upload_status == UploadStatus.PENDING.value,
            )
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def last_successful(self) -> BackupHistory | None:
        statement = (
            select(BackupHistory)
            .where(BackupHistory.status == BackupStatus.SUCCESS.value)
            .order_by(BackupHistory.finished_at.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    # One keyword per query parameter: the filter set is the API's, and collapsing it into a
    # dict would trade a type error at build time for a typo at runtime.
    @staticmethod
    def _filtered[T: tuple[Any, ...]](
        *,
        vmid: int | None,
        guest_type: GuestType | None,
        status: BackupStatus | None,
        upload_status: UploadStatus | None,
        run_id: int | None,
        since: datetime | None,
        until: datetime | None,
        query: str | None,
        include_deleted: bool,
        base: Select[T],
    ) -> Select[T]:
        statement = base
        if vmid is not None:
            statement = statement.where(BackupHistory.vmid == vmid)
        if guest_type is not None:
            statement = statement.where(BackupHistory.guest_type == guest_type.value)
        if status is not None:
            statement = statement.where(BackupHistory.status == status.value)
        if upload_status is not None:
            statement = statement.where(BackupHistory.upload_status == upload_status.value)
        if run_id is not None:
            statement = statement.where(BackupHistory.run_id == run_id)
        if since is not None:
            statement = statement.where(BackupHistory.started_at >= since)
        if until is not None:
            statement = statement.where(BackupHistory.started_at <= until)
        if not include_deleted:
            # A deleted artifact stays in the table as an audit record; it is not a backup
            # anyone can restore from, so it is out of the default view.
            statement = statement.where(BackupHistory.status != BackupStatus.DELETED.value)
        if query:
            pattern = f"%{query.strip()}%"
            conditions: list[ColumnElement[bool]] = [
                BackupHistory.guest_name.ilike(pattern),
                BackupHistory.filename.ilike(pattern),
            ]
            if query.strip().isdigit():
                conditions.append(BackupHistory.vmid == int(query.strip()))
            statement = statement.where(or_(*conditions))
        return statement
