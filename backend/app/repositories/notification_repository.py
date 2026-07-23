"""Outbox persistence.

The queue is the table, exactly as it is for backup runs, transfers and restores. A row is
written in the same transaction as the state change it reports, so a crash between "the backup
failed" and "the operator was told" is impossible: either both are durable or neither is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import Select, func, or_, select

from app.db.models.system import Notification
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import NotificationChannel, NotificationEvent, NotificationStatus


class NotificationRepository(Protocol):
    async def get(self, notification_id: int) -> Notification | None: ...
    async def create(
        self,
        *,
        channel: NotificationChannel,
        event: NotificationEvent,
        status: NotificationStatus,
        payload: dict[str, Any],
        max_attempts: int,
        dedupe_key: str | None = None,
        backup_id: int | None = None,
        restore_id: int | None = None,
        correlation_id: str | None = None,
    ) -> Notification: ...
    async def search(
        self,
        *,
        status: NotificationStatus | None = None,
        event: NotificationEvent | None = None,
        channel: NotificationChannel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]: ...
    async def count(self, **filters: Any) -> int: ...
    async def next_ready(self, *, now: datetime) -> Notification | None: ...
    async def latest_with_key(
        self,
        *,
        channel: NotificationChannel,
        event: NotificationEvent,
        dedupe_key: str,
        since: datetime | None = None,
        statuses: Sequence[NotificationStatus] | None = None,
    ) -> Notification | None: ...
    async def pending_count(self) -> int: ...


class SqlAlchemyNotificationRepository(SqlAlchemyRepository):
    async def get(self, notification_id: int) -> Notification | None:
        return await self._session.get(Notification, notification_id)

    async def create(
        self,
        *,
        channel: NotificationChannel,
        event: NotificationEvent,
        status: NotificationStatus,
        payload: dict[str, Any],
        max_attempts: int,
        dedupe_key: str | None = None,
        backup_id: int | None = None,
        restore_id: int | None = None,
        correlation_id: str | None = None,
    ) -> Notification:
        record = Notification(
            channel=channel.value,
            event_type=event.value,
            status=status.value,
            payload=payload,
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
            backup_id=backup_id,
            restore_id=restore_id,
            correlation_id=correlation_id,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    def _filtered(
        self,
        *,
        status: NotificationStatus | None = None,
        event: NotificationEvent | None = None,
        channel: NotificationChannel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Select[tuple[Notification]]:
        statement = select(Notification)
        if status is not None:
            statement = statement.where(Notification.status == status.value)
        if event is not None:
            statement = statement.where(Notification.event_type == event.value)
        if channel is not None:
            statement = statement.where(Notification.channel == channel.value)
        if since is not None:
            statement = statement.where(Notification.created_at >= since)
        if until is not None:
            statement = statement.where(Notification.created_at <= until)
        return statement

    async def search(
        self,
        *,
        status: NotificationStatus | None = None,
        event: NotificationEvent | None = None,
        channel: NotificationChannel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        statement = (
            self._filtered(status=status, event=event, channel=channel, since=since, until=until)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(statement)).scalars())

    async def count(self, **filters: Any) -> int:
        statement = select(func.count()).select_from(self._filtered(**filters).subquery())
        return int((await self._session.execute(statement)).scalar_one())

    async def next_ready(self, *, now: datetime) -> Notification | None:
        """The oldest pending message whose backoff has elapsed.

        Oldest first, unlike the listing endpoints: a message queued during an outage must not
        be starved by the ones that arrive after the outage clears.
        """
        statement = (
            select(Notification)
            .where(
                Notification.status == NotificationStatus.PENDING.value,
                or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now),
            )
            .order_by(Notification.created_at.asc(), Notification.id.asc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def latest_with_key(
        self,
        *,
        channel: NotificationChannel,
        event: NotificationEvent,
        dedupe_key: str,
        since: datetime | None = None,
        statuses: Sequence[NotificationStatus] | None = None,
    ) -> Notification | None:
        """The most recent row describing this same occurrence, if there is one."""
        statement = select(Notification).where(
            Notification.channel == channel.value,
            Notification.event_type == event.value,
            Notification.dedupe_key == dedupe_key,
        )
        if since is not None:
            statement = statement.where(Notification.created_at >= since)
        if statuses:
            statement = statement.where(
                Notification.status.in_([status.value for status in statuses])
            )
        statement = statement.order_by(
            Notification.created_at.desc(), Notification.id.desc()
        ).limit(1)
        return (await self._session.execute(statement)).scalars().first()

    async def pending_count(self) -> int:
        return await self.count(status=NotificationStatus.PENDING)
