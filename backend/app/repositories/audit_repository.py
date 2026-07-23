"""Audit trail persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, Select, delete, func, or_, select

from app.core.logging import get_correlation_id
from app.db.models.system import AuditEvent
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import AuditAction, AuditResult


class AuditRepository(Protocol):
    async def record(
        self,
        *,
        action: AuditAction,
        result: AuditResult = AuditResult.SUCCESS,
        user_id: int | None = None,
        username: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent: ...
    async def recent(self, *, limit: int = 50) -> list[AuditEvent]: ...
    async def for_user(self, user_id: int, *, limit: int = 50) -> list[AuditEvent]: ...
    async def count_since(self, action: AuditAction, *, since: datetime) -> int: ...
    async def search(
        self,
        *,
        action: AuditAction | None = None,
        result: AuditResult | None = None,
        user_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]: ...
    async def count(self, **filters: Any) -> int: ...
    async def prune(self, *, before: datetime) -> int: ...


class SqlAlchemyAuditRepository(SqlAlchemyRepository):
    async def record(
        self,
        *,
        action: AuditAction,
        result: AuditResult = AuditResult.SUCCESS,
        user_id: int | None = None,
        username: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            action=action.value,
            result=result.value,
            user_id=user_id,
            username_snapshot=username,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            user_agent=user_agent[:255] if user_agent else None,
            detail=detail,
            correlation_id=get_correlation_id(),
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def recent(self, *, limit: int = 50) -> list[AuditEvent]:
        statement = select(AuditEvent).order_by(AuditEvent.ts.desc()).limit(limit)
        return list((await self._session.execute(statement)).scalars())

    async def for_user(self, user_id: int, *, limit: int = 50) -> list[AuditEvent]:
        statement = (
            select(AuditEvent)
            .where(AuditEvent.user_id == user_id)
            .order_by(AuditEvent.ts.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars())

    async def count_since(self, action: AuditAction, *, since: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == action.value, AuditEvent.ts >= since)
        )
        return int((await self._session.execute(statement)).scalar_one())

    def _filtered(
        self,
        *,
        action: AuditAction | None = None,
        result: AuditResult | None = None,
        user_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
    ) -> Select[tuple[AuditEvent]]:
        statement = select(AuditEvent)
        if action is not None:
            statement = statement.where(AuditEvent.action == action.value)
        if result is not None:
            statement = statement.where(AuditEvent.result == result.value)
        if user_id is not None:
            statement = statement.where(AuditEvent.user_id == user_id)
        if since is not None:
            statement = statement.where(AuditEvent.ts >= since)
        if until is not None:
            statement = statement.where(AuditEvent.ts <= until)
        if correlation_id:
            statement = statement.where(AuditEvent.correlation_id == correlation_id)
        if query:
            # The trail's free text is who did it and what it was done to; `detail` is JSON and
            # deliberately excluded, as it is for `logs`.
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    AuditEvent.username_snapshot.ilike(pattern),
                    AuditEvent.resource_type.ilike(pattern),
                    AuditEvent.resource_id.ilike(pattern),
                    AuditEvent.action.ilike(pattern),
                )
            )
        return statement

    async def search(
        self,
        *,
        action: AuditAction | None = None,
        result: AuditResult | None = None,
        user_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        statement = (
            self._filtered(
                action=action,
                result=result,
                user_id=user_id,
                since=since,
                until=until,
                query=query,
                correlation_id=correlation_id,
            )
            .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(statement)).scalars())

    async def count(self, **filters: Any) -> int:
        statement = select(func.count()).select_from(self._filtered(**filters).subquery())
        return int((await self._session.execute(statement)).scalar_one())

    async def prune(self, *, before: datetime) -> int:
        """Audit rows outlive application logs by design — `audit_retention_days` defaults to
        two years against `log_retention_days`' ninety days — but they are not immortal."""
        result = await self._session.execute(delete(AuditEvent).where(AuditEvent.ts < before))
        return int(cast("CursorResult[Any]", result).rowcount or 0)
