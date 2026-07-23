"""Refresh-token persistence, including family revocation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, delete, select, update

from app.db.models.user import RefreshToken
from app.repositories.base import SqlAlchemyRepository


class RefreshTokenRepository(Protocol):
    async def create(
        self,
        *,
        user_id: int,
        token_hash: str,
        family_id: str,
        expires_at: datetime,
        created_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> RefreshToken: ...
    async def get_by_hash(self, token_hash: str) -> RefreshToken | None: ...
    async def revoke(self, token: RefreshToken, *, at: datetime, reason: str) -> None: ...
    async def revoke_family(self, family_id: str, *, at: datetime, reason: str) -> int: ...
    async def revoke_all_for_user(
        self, user_id: int, *, at: datetime, reason: str, except_family: str | None = None
    ) -> int: ...
    async def list_active_for_user(self, user_id: int, *, now: datetime) -> list[RefreshToken]: ...
    async def purge_expired(self, *, before: datetime) -> int: ...


class SqlAlchemyRefreshTokenRepository(SqlAlchemyRepository):
    async def create(
        self,
        *,
        user_id: int,
        token_hash: str,
        family_id: str,
        expires_at: datetime,
        created_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> RefreshToken:
        token = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=expires_at,
            created_at=created_at,
            user_agent=user_agent[:255] if user_agent else None,
            ip_address=ip_address,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def revoke(self, token: RefreshToken, *, at: datetime, reason: str) -> None:
        if token.revoked_at is None:
            token.revoked_at = at
            token.revoked_reason = reason
        await self._session.flush()

    async def revoke_family(self, family_id: str, *, at: datetime, reason: str) -> int:
        statement = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=at, revoked_reason=reason)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        await self._session.flush()
        return int(result.rowcount or 0)

    async def revoke_all_for_user(
        self, user_id: int, *, at: datetime, reason: str, except_family: str | None = None
    ) -> int:
        conditions = [RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)]
        if except_family is not None:
            conditions.append(RefreshToken.family_id != except_family)
        statement = (
            update(RefreshToken).where(*conditions).values(revoked_at=at, revoked_reason=reason)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        await self._session.flush()
        return int(result.rowcount or 0)

    async def list_active_for_user(self, user_id: int, *, now: datetime) -> list[RefreshToken]:
        statement = (
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .order_by(RefreshToken.created_at.desc())
        )
        return list((await self._session.execute(statement)).scalars())

    async def purge_expired(self, *, before: datetime) -> int:
        statement = delete(RefreshToken).where(RefreshToken.expires_at < before)
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return int(result.rowcount or 0)
