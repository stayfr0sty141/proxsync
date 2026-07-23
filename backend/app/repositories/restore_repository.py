"""Restore persistence.

`restore_history` is both the audit record and the work queue: a row exists from the moment a
restore is *requested*, and the executor claims rows in `confirmed`. That is what makes the
two-phase flow crash-safe — a dashboard that dies between confirmation and execution restarts
with the intent still on disk, and one that dies mid-restore restarts with a row that says so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import Select, func, select

from app.db.models.backup import RestoreHistory
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import GuestType, RestoreMode, RestoreSource, RestoreStatus

ACTIVE_STATES = tuple(status.value for status in RestoreStatus if status.blocks_retention)
"""Derived from the enum rather than restated, so the retention blocker and the queue can
never disagree about what "active" means."""

IN_FLIGHT_STATES = (RestoreStatus.CONFIRMED.value, RestoreStatus.RUNNING.value)
"""Authorised to touch the host. A `pending_confirmation` row is a proposal, not a restore,
so it blocks retention but does not stop another restore from being requested."""

EXPIRY_MESSAGE = "The confirmation window closed before it was confirmed"


class RestoreRepository(Protocol):
    async def get(self, restore_id: int) -> RestoreHistory | None: ...
    async def create(
        self,
        *,
        backup_id: int,
        source: RestoreSource,
        target_vmid: int,
        target_type: GuestType,
        restore_mode: RestoreMode,
        target_node: str,
        target_storage: str,
        overwrite_existing: bool,
        force_stop: bool,
        start_after_restore: bool,
        confirmation_token_hash: str,
        confirmation_expires_at: datetime,
        preflight: dict[str, object],
        requested_by: int | None,
        correlation_id: str,
    ) -> RestoreHistory: ...
    async def next_confirmed(self) -> RestoreHistory | None: ...
    async def in_flight(self) -> RestoreHistory | None: ...
    async def running(self) -> list[RestoreHistory]: ...
    async def expired_pending(self, *, now: datetime) -> list[RestoreHistory]: ...
    async def expire_pending(self, *, now: datetime) -> list[int]: ...
    async def active_for_backup(self, backup_id: int) -> RestoreHistory | None: ...
    async def search(
        self,
        *,
        status: RestoreStatus | None = None,
        backup_id: int | None = None,
        target_vmid: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RestoreHistory]: ...
    async def count(
        self,
        *,
        status: RestoreStatus | None = None,
        backup_id: int | None = None,
        target_vmid: int | None = None,
    ) -> int: ...


class SqlAlchemyRestoreRepository(SqlAlchemyRepository):
    async def get(self, restore_id: int) -> RestoreHistory | None:
        return await self._session.get(RestoreHistory, restore_id)

    async def create(
        self,
        *,
        backup_id: int,
        source: RestoreSource,
        target_vmid: int,
        target_type: GuestType,
        restore_mode: RestoreMode,
        target_node: str,
        target_storage: str,
        overwrite_existing: bool,
        force_stop: bool,
        start_after_restore: bool,
        confirmation_token_hash: str,
        confirmation_expires_at: datetime,
        preflight: dict[str, object],
        requested_by: int | None,
        correlation_id: str,
    ) -> RestoreHistory:
        record = RestoreHistory(
            backup_id=backup_id,
            source=source.value,
            target_vmid=target_vmid,
            target_type=target_type.value,
            restore_mode=restore_mode.value,
            target_node=target_node,
            target_storage=target_storage,
            overwrite_existing=overwrite_existing,
            force_stop=force_stop,
            start_after_restore=start_after_restore,
            status=RestoreStatus.PENDING_CONFIRMATION.value,
            confirmation_token_hash=confirmation_token_hash,
            confirmation_expires_at=confirmation_expires_at,
            preflight=preflight,
            requested_by=requested_by,
            correlation_id=correlation_id,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def next_confirmed(self) -> RestoreHistory | None:
        """The oldest confirmed restore. Ascending by id: a queue is not a stack."""
        statement = (
            select(RestoreHistory)
            .where(RestoreHistory.status == RestoreStatus.CONFIRMED.value)
            .order_by(RestoreHistory.id)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def in_flight(self) -> RestoreHistory | None:
        """Any restore already authorised to run. Restores are serialised globally."""
        statement = (
            select(RestoreHistory)
            .where(RestoreHistory.status.in_(IN_FLIGHT_STATES))
            .order_by(RestoreHistory.id)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def running(self) -> list[RestoreHistory]:
        """Rows a restart has to reconcile."""
        statement = (
            select(RestoreHistory)
            .where(RestoreHistory.status == RestoreStatus.RUNNING.value)
            .order_by(RestoreHistory.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def expired_pending(self, *, now: datetime) -> list[RestoreHistory]:
        """Unconfirmed requests whose window has closed.

        They must be swept rather than left alone: an active restore blocks retention from
        deleting the artifact it names, so an abandoned dialog would pin a backup forever.
        """
        statement = (
            select(RestoreHistory)
            .where(
                RestoreHistory.status == RestoreStatus.PENDING_CONFIRMATION.value,
                RestoreHistory.confirmation_expires_at.is_not(None),
                RestoreHistory.confirmation_expires_at <= now,
            )
            .order_by(RestoreHistory.id)
        )
        return list((await self._session.execute(statement)).scalars())

    async def expire_pending(self, *, now: datetime) -> list[int]:
        """Close every window that has elapsed. Returns the ids that changed.

        The transition lives here rather than in the service because two callers need it —
        the API's sweep and the executor's — and two copies of "what expired means" is exactly
        how a row ends up blocking retention with a status that says it should not.
        """
        expired = await self.expired_pending(now=now)
        for record in expired:
            record.status = RestoreStatus.EXPIRED.value
            record.finished_at = now
            record.confirmation_token_hash = None
            record.error_message = EXPIRY_MESSAGE
        if expired:
            await self.flush()
        return [record.id for record in expired]

    async def active_for_backup(self, backup_id: int) -> RestoreHistory | None:
        statement = (
            select(RestoreHistory)
            .where(
                RestoreHistory.backup_id == backup_id,
                RestoreHistory.status.in_(ACTIVE_STATES),
            )
            .order_by(RestoreHistory.id)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def search(
        self,
        *,
        status: RestoreStatus | None = None,
        backup_id: int | None = None,
        target_vmid: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RestoreHistory]:
        statement = self._filtered(
            status=status,
            backup_id=backup_id,
            target_vmid=target_vmid,
            base=select(RestoreHistory),
        ).order_by(RestoreHistory.created_at.desc(), RestoreHistory.id.desc())
        return list((await self._session.execute(statement.limit(limit).offset(offset))).scalars())

    async def count(
        self,
        *,
        status: RestoreStatus | None = None,
        backup_id: int | None = None,
        target_vmid: int | None = None,
    ) -> int:
        statement = self._filtered(
            status=status,
            backup_id=backup_id,
            target_vmid=target_vmid,
            base=select(func.count()).select_from(RestoreHistory),
        )
        return int((await self._session.execute(statement)).scalar_one())

    @staticmethod
    def _filtered[T: tuple[object, ...]](
        *,
        status: RestoreStatus | None,
        backup_id: int | None,
        target_vmid: int | None,
        base: Select[T],
    ) -> Select[T]:
        statement = base
        if status is not None:
            statement = statement.where(RestoreHistory.status == status.value)
        if backup_id is not None:
            statement = statement.where(RestoreHistory.backup_id == backup_id)
        if target_vmid is not None:
            statement = statement.where(RestoreHistory.target_vmid == target_vmid)
        return statement
