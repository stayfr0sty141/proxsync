"""Guest inventory persistence.

Rows are never deleted when a guest disappears from Proxmox. `backup_history` keeps its own
denormalised copy of the name, but the guest row is what the UI joins against to show "this
VM no longer exists on the host" instead of a blank cell.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import ColumnElement, Select, func, or_, select

from app.db.models.guest import Guest
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import GuestStatus, GuestType


class GuestRepository(Protocol):
    async def get(self, guest_id: int) -> Guest | None: ...
    async def get_by_vmid(self, vmid: int, guest_type: GuestType) -> Guest | None: ...
    async def list_all(self) -> list[Guest]: ...
    async def search(
        self,
        *,
        guest_type: GuestType | None = None,
        status: GuestStatus | None = None,
        backup_enabled: bool | None = None,
        query: str | None = None,
    ) -> list[Guest]: ...
    async def enabled_guests(self) -> list[Guest]: ...
    async def add(self, guest: Guest) -> Guest: ...
    async def count(self) -> int: ...
    async def count_by_type(self, guest_type: GuestType) -> int: ...


class SqlAlchemyGuestRepository(SqlAlchemyRepository):
    async def get(self, guest_id: int) -> Guest | None:
        return await self._session.get(Guest, guest_id)

    async def get_by_vmid(self, vmid: int, guest_type: GuestType) -> Guest | None:
        statement = select(Guest).where(Guest.vmid == vmid, Guest.guest_type == guest_type.value)
        return (await self._session.execute(statement)).scalars().first()

    async def list_all(self) -> list[Guest]:
        return list((await self._session.execute(self._ordered())).scalars())

    async def search(
        self,
        *,
        guest_type: GuestType | None = None,
        status: GuestStatus | None = None,
        backup_enabled: bool | None = None,
        query: str | None = None,
    ) -> list[Guest]:
        statement = self._ordered()
        if guest_type is not None:
            statement = statement.where(Guest.guest_type == guest_type.value)
        if status is not None:
            statement = statement.where(Guest.status == status.value)
        if backup_enabled is not None:
            statement = statement.where(Guest.backup_enabled.is_(backup_enabled))
        if query:
            pattern = f"%{query.strip()}%"
            # A search box that only matched names would be useless to an operator who thinks
            # in VMIDs, so numeric input matches the id as well.
            conditions: list[ColumnElement[bool]] = [Guest.name.ilike(pattern)]
            if query.strip().isdigit():
                conditions.append(Guest.vmid == int(query.strip()))
            statement = statement.where(or_(*conditions))
        return list((await self._session.execute(statement)).scalars())

    async def enabled_guests(self) -> list[Guest]:
        statement = self._ordered().where(Guest.backup_enabled.is_(True))
        return list((await self._session.execute(statement)).scalars())

    async def add(self, guest: Guest) -> Guest:
        self._session.add(guest)
        await self._session.flush()
        return guest

    async def count(self) -> int:
        statement = select(func.count()).select_from(Guest)
        return int((await self._session.execute(statement)).scalar_one())

    async def count_by_type(self, guest_type: GuestType) -> int:
        statement = (
            select(func.count()).select_from(Guest).where(Guest.guest_type == guest_type.value)
        )
        return int((await self._session.execute(statement)).scalar_one())

    @staticmethod
    def _ordered() -> Select[tuple[Guest]]:
        return select(Guest).order_by(Guest.vmid)
