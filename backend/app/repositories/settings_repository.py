"""Settings persistence.

Values are stored wrapped as ``{"v": <value>}``. SQLite's JSON1 and PostgreSQL's JSONB both
accept bare scalars, but wrapping keeps the column shape uniform and leaves room to attach
per-value metadata later without a migration.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models.system import Setting
from app.repositories.base import SqlAlchemyRepository
from app.schemas.enums import SettingsSection, SettingValueType

_WRAPPER_KEY = "v"


def wrap(value: Any) -> dict[str, Any]:
    return {_WRAPPER_KEY: value}


def unwrap(stored: dict[str, Any] | None) -> Any:
    if stored is None:
        return None
    return stored.get(_WRAPPER_KEY)


class SettingsRepository(Protocol):
    async def list_section(self, section: SettingsSection) -> list[Setting]: ...
    async def list_all(self) -> list[Setting]: ...
    async def get(self, section: SettingsSection, key: str) -> Setting | None: ...
    async def upsert(
        self,
        *,
        section: SettingsSection,
        key: str,
        value: Any,
        value_type: SettingValueType,
        is_secret: bool = False,
        updated_by: int | None = None,
    ) -> Setting: ...


class SqlAlchemySettingsRepository(SqlAlchemyRepository):
    async def list_section(self, section: SettingsSection) -> list[Setting]:
        statement = select(Setting).where(Setting.section == section.value).order_by(Setting.key)
        return list((await self._session.execute(statement)).scalars())

    async def list_all(self) -> list[Setting]:
        statement = select(Setting).order_by(Setting.section, Setting.key)
        return list((await self._session.execute(statement)).scalars())

    async def get(self, section: SettingsSection, key: str) -> Setting | None:
        statement = select(Setting).where(Setting.section == section.value, Setting.key == key)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        section: SettingsSection,
        key: str,
        value: Any,
        value_type: SettingValueType,
        is_secret: bool = False,
        updated_by: int | None = None,
    ) -> Setting:
        existing = await self.get(section, key)
        if existing is None:
            existing = Setting(section=section.value, key=key)
            self._session.add(existing)

        existing.value = wrap(value)
        existing.value_type = value_type.value
        existing.is_secret = is_secret
        existing.updated_by = updated_by
        existing.updated_at = utcnow()
        await self._session.flush()
        return existing
