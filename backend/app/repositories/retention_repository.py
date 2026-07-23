"""Persistence operations used by the retention policy.

The repository returns immutable projections rather than live ORM rows. Retention performs
network I/O between deciding and recording an outcome; detached values make it explicit that
the candidate must be re-read before its soft-delete marker is changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import case, or_, select

from app.core.logging import get_correlation_id
from app.db.models.backup import (
    BackupHistory,
    BackupJob,
    BackupRun,
    RestoreHistory,
    RetentionEvent,
)
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import (
    BackupStatus,
    GuestType,
    RestoreStatus,
    RetentionAction,
    TriggerType,
    UploadStatus,
)

ACTIVE_RESTORE_STATES = (
    RestoreStatus.PENDING_CONFIRMATION.value,
    RestoreStatus.CONFIRMED.value,
    RestoreStatus.RUNNING.value,
)


@dataclass(frozen=True, slots=True)
class RetentionBackup:
    id: int
    vmid: int
    guest_type: GuestType
    guest_name: str
    filename: str | None
    size_bytes: int | None
    remote_size_bytes: int | None
    upload_status: UploadStatus
    retention_locked: bool
    started_at: datetime | None
    local_deleted_at: datetime | None
    remote_deleted_at: datetime | None
    remote_path: str | None
    correlation_id: str | None


@dataclass(frozen=True, slots=True)
class RetentionPolicyContext:
    """Raw run provenance plus the surviving linked job's current retention counts."""

    run_id: int
    trigger: TriggerType
    job_id: int | None
    options: dict[str, Any] | None
    job_keep_local: int | None
    job_keep_remote: int | None


class RetentionRepository(Protocol):
    async def guest_for_backup(self, backup_id: int) -> tuple[int, GuestType] | None: ...
    async def run_options_for_backup(self, backup_id: int) -> dict[str, Any] | None: ...
    async def retention_policy_context(self, backup_id: int) -> RetentionPolicyContext | None: ...
    async def canonical_trigger_backup_id(self, backup_id: int) -> int | None: ...
    async def newest_trigger_backup_ids(self) -> list[int]: ...
    async def guest_keys(self) -> list[tuple[int, GuestType]]: ...
    async def local_for_guest(self, vmid: int, guest_type: GuestType) -> list[RetentionBackup]: ...
    async def remote_for_guest(self, vmid: int, guest_type: GuestType) -> list[RetentionBackup]: ...
    async def has_active_restore(self, backup_id: int) -> bool: ...
    async def mark_local_deleted(self, backup_id: int, *, deleted_at: datetime) -> None: ...
    async def mark_remote_deleted(self, backup_id: int, *, deleted_at: datetime) -> None: ...
    async def record_event(
        self,
        *,
        backup: RetentionBackup,
        action: RetentionAction,
        reason: str,
        policy_snapshot: dict[str, Any],
        freed_bytes: int,
        dry_run: bool,
    ) -> None: ...
    async def commit(self) -> None: ...


class SqlAlchemyRetentionRepository(SqlAlchemyRepository):
    async def guest_for_backup(self, backup_id: int) -> tuple[int, GuestType] | None:
        statement = select(BackupHistory.vmid, BackupHistory.guest_type).where(
            BackupHistory.id == backup_id
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return int(row.vmid), GuestType(row.guest_type)

    async def run_options_for_backup(self, backup_id: int) -> dict[str, Any] | None:
        context = await self.retention_policy_context(backup_id)
        return (
            dict(context.options) if context is not None and context.options is not None else None
        )

    async def retention_policy_context(self, backup_id: int) -> RetentionPolicyContext | None:
        """Return raw provenance without allowing Pydantic defaults to mask legacy rows."""

        statement = (
            select(
                BackupRun.id.label("run_id"),
                BackupRun.trigger,
                BackupRun.job_id,
                BackupRun.options,
                BackupJob.keep_local.label("job_keep_local"),
                BackupJob.keep_remote.label("job_keep_remote"),
            )
            .join(BackupHistory, BackupHistory.run_id == BackupRun.id)
            .outerjoin(BackupJob, BackupRun.job_id == BackupJob.id)
            .where(BackupHistory.id == backup_id)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RetentionPolicyContext(
            run_id=int(row.run_id),
            trigger=TriggerType(row.trigger),
            job_id=int(row.job_id) if row.job_id is not None else None,
            options=dict(row.options) if isinstance(row.options, dict) else None,
            job_keep_local=(int(row.job_keep_local) if row.job_keep_local is not None else None),
            job_keep_remote=(int(row.job_keep_remote) if row.job_keep_remote is not None else None),
        )

    async def canonical_trigger_backup_id(self, backup_id: int) -> int | None:
        """Map a possibly stale notification to the guest's newest existing copy."""

        guest = await self.guest_for_backup(backup_id)
        if guest is None:
            return None
        vmid, guest_type = guest
        statement = (
            select(BackupHistory.id)
            .where(
                BackupHistory.vmid == vmid,
                BackupHistory.guest_type == guest_type.value,
                _existing_copy_clause(),
            )
            .order_by(*_newest_first())
            .limit(1)
        )
        result = (await self._session.execute(statement)).scalar_one_or_none()
        return int(result) if result is not None else None

    async def newest_trigger_backup_ids(self) -> list[int]:
        """Newest existing-copy row per guest, used to recover missed startup triggers."""

        statement = (
            select(BackupHistory.id, BackupHistory.vmid, BackupHistory.guest_type)
            .where(_existing_copy_clause())
            .order_by(
                BackupHistory.vmid,
                BackupHistory.guest_type,
                *_newest_first(),
            )
        )
        rows = (await self._session.execute(statement)).all()
        seen: set[tuple[int, str]] = set()
        trigger_ids: list[int] = []
        for row in rows:
            guest = int(row.vmid), str(row.guest_type)
            if guest in seen:
                continue
            seen.add(guest)
            trigger_ids.append(int(row.id))
        return trigger_ids

    async def guest_keys(self) -> list[tuple[int, GuestType]]:
        """Guests with at least one real local or remote copy that retention can evaluate."""

        statement = (
            select(BackupHistory.vmid, BackupHistory.guest_type)
            .where(_existing_copy_clause())
            .distinct()
            .order_by(BackupHistory.vmid, BackupHistory.guest_type)
        )
        rows = (await self._session.execute(statement)).all()
        return [(int(row.vmid), GuestType(row.guest_type)) for row in rows]

    async def local_for_guest(self, vmid: int, guest_type: GuestType) -> list[RetentionBackup]:
        statement = (
            select(BackupHistory)
            .where(
                BackupHistory.vmid == vmid,
                BackupHistory.guest_type == guest_type.value,
                BackupHistory.status == BackupStatus.SUCCESS.value,
                BackupHistory.local_deleted_at.is_(None),
                BackupHistory.filename.is_not(None),
            )
            .order_by(*_newest_first())
        )
        rows = (await self._session.execute(statement)).scalars()
        return [_project(row) for row in rows]

    async def remote_for_guest(self, vmid: int, guest_type: GuestType) -> list[RetentionBackup]:
        """Only actual uploaded objects count toward the remote keep slots.

        `not_required` is deliberately absent: it means no remote object was expected and must
        never evict one that really exists. `deleted` status is accepted for legacy rows whose
        local copy was removed before location-independent status semantics were introduced.
        """

        statement = (
            select(BackupHistory)
            .where(
                BackupHistory.vmid == vmid,
                BackupHistory.guest_type == guest_type.value,
                BackupHistory.status.in_((BackupStatus.SUCCESS.value, BackupStatus.DELETED.value)),
                BackupHistory.upload_status == UploadStatus.UPLOADED.value,
                BackupHistory.remote_deleted_at.is_(None),
                BackupHistory.remote_path.is_not(None),
                BackupHistory.filename.is_not(None),
            )
            .order_by(*_newest_first())
        )
        rows = (await self._session.execute(statement)).scalars()
        return [_project(row) for row in rows]

    async def has_active_restore(self, backup_id: int) -> bool:
        statement = (
            select(RestoreHistory.id)
            .where(
                RestoreHistory.backup_id == backup_id,
                RestoreHistory.status.in_(ACTIVE_RESTORE_STATES),
            )
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def mark_local_deleted(self, backup_id: int, *, deleted_at: datetime) -> None:
        record = await self._session.get(BackupHistory, backup_id)
        if record is None or record.local_deleted_at is not None:
            return
        record.local_deleted_at = deleted_at
        _refresh_status(record)
        await self._session.flush()

    async def mark_remote_deleted(self, backup_id: int, *, deleted_at: datetime) -> None:
        record = await self._session.get(BackupHistory, backup_id)
        if record is None or record.remote_deleted_at is not None:
            return
        record.remote_deleted_at = deleted_at
        _refresh_status(record)
        await self._session.flush()

    async def record_event(
        self,
        *,
        backup: RetentionBackup,
        action: RetentionAction,
        reason: str,
        policy_snapshot: dict[str, Any],
        freed_bytes: int,
        dry_run: bool,
    ) -> None:
        self._session.add(
            RetentionEvent(
                backup_id=backup.id,
                vmid=backup.vmid,
                guest_type=backup.guest_type.value,
                action=action.value,
                reason=reason[:255],
                policy_snapshot=policy_snapshot,
                freed_bytes=max(freed_bytes, 0),
                dry_run=dry_run,
                correlation_id=get_correlation_id() or backup.correlation_id,
            )
        )
        await self._session.flush()

    async def commit(self) -> None:
        """Close the current transaction before any caller performs agent network I/O."""

        await self._session.commit()
        self._session.expire_all()


def _newest_first() -> tuple[Any, ...]:
    """Portable NULL-last, then stable timestamp/id ordering.

    PostgreSQL and SQLite disagree about where NULL sorts by default. The CASE expression makes
    the ordering identical and the id tie-break prevents two backups with the same timestamp
    from swapping keep/candidate roles between evaluations.
    """

    missing_timestamp = case((BackupHistory.started_at.is_(None), 1), else_=0)
    return missing_timestamp, BackupHistory.started_at.desc(), BackupHistory.id.desc()


def _existing_copy_clause() -> Any:
    local_exists = (
        (BackupHistory.status == BackupStatus.SUCCESS.value)
        & BackupHistory.local_deleted_at.is_(None)
        & BackupHistory.filename.is_not(None)
    )
    remote_exists = (
        BackupHistory.status.in_((BackupStatus.SUCCESS.value, BackupStatus.DELETED.value))
        & (BackupHistory.upload_status == UploadStatus.UPLOADED.value)
        & BackupHistory.remote_deleted_at.is_(None)
        & BackupHistory.remote_path.is_not(None)
        & BackupHistory.filename.is_not(None)
    )
    return or_(local_exists, remote_exists)


def _project(record: BackupHistory) -> RetentionBackup:
    return RetentionBackup(
        id=record.id,
        vmid=record.vmid,
        guest_type=GuestType(record.guest_type),
        guest_name=record.guest_name,
        filename=record.filename,
        size_bytes=record.size_bytes,
        remote_size_bytes=record.remote_size_bytes,
        upload_status=UploadStatus(record.upload_status),
        retention_locked=record.retention_locked,
        started_at=record.started_at,
        local_deleted_at=record.local_deleted_at,
        remote_deleted_at=record.remote_deleted_at,
        remote_path=record.remote_path,
        correlation_id=record.correlation_id,
    )


def _refresh_status(record: BackupHistory) -> None:
    """A history row is deleted only once neither location contains a usable copy."""

    local_exists = record.local_deleted_at is None and record.filename is not None
    remote_exists = record.remote_path is not None and record.remote_deleted_at is None
    record.status = (
        BackupStatus.SUCCESS.value if local_exists or remote_exists else BackupStatus.DELETED.value
    )
