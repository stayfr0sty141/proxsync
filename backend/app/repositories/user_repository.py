"""User persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select

from app.db.models.user import User
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import UserRole


def normalise_username(username: str) -> str:
    """Usernames are compared case-insensitively; the canonical form is lowercase.

    `casefold` rather than `lower`, so non-ASCII usernames compare the way a human expects.
    """
    return username.strip().casefold()


class UserRepository(Protocol):
    async def get(self, user_id: int) -> User | None: ...
    async def get_by_username(self, username: str) -> User | None: ...
    async def list_all(self) -> list[User]: ...
    async def count(self) -> int: ...
    async def add(self, user: User) -> User: ...
    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        role: UserRole,
        email: str | None = None,
        must_change_password: bool = False,
        is_active: bool = True,
    ) -> User: ...
    async def record_login_success(self, user: User, *, at: datetime, ip: str | None) -> None: ...
    async def record_login_failure(
        self, user: User, *, failed_count: int, locked_until: datetime | None
    ) -> None: ...


class SqlAlchemyUserRepository(SqlAlchemyRepository):
    async def get(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        statement = select(User).where(User.username == normalise_username(username))
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_all(self) -> list[User]:
        statement = select(User).order_by(User.username)
        return list((await self._session.execute(statement)).scalars())

    async def count(self) -> int:
        statement = select(func.count()).select_from(User)
        return int((await self._session.execute(statement)).scalar_one())

    async def add(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user

    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        role: UserRole,
        email: str | None = None,
        must_change_password: bool = False,
        is_active: bool = True,
    ) -> User:
        user = User(
            username=normalise_username(username),
            email=email,
            password_hash=password_hash,
            role=role.value,
            must_change_password=must_change_password,
            is_active=is_active,
        )
        return await self.add(user)

    async def record_login_success(self, user: User, *, at: datetime, ip: str | None) -> None:
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = at
        user.last_login_ip = ip
        await self._session.flush()

    async def record_login_failure(
        self, user: User, *, failed_count: int, locked_until: datetime | None
    ) -> None:
        user.failed_login_count = failed_count
        user.locked_until = locked_until
        await self._session.flush()
