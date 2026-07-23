"""Persistence-layer behaviour and PostgreSQL-portability invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.models import BackupHistory, Guest, RefreshToken, Setting, User
from app.repositories.settings_repository import SqlAlchemySettingsRepository, unwrap
from app.repositories.user_repository import SqlAlchemyUserRepository, normalise_username
from app.schemas.enums import SettingsSection, SettingValueType, UserRole


class TestPrimaryKeys:
    async def test_ids_autoincrement(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression: a plain `BigInteger` primary key is not a rowid alias on SQLite, so
        every insert failed with 'NOT NULL constraint failed'."""
        async with session_factory() as session:
            first = User(username="one", password_hash="x", role="viewer")
            second = User(username="two", password_hash="x", role="viewer")
            session.add_all([first, second])
            await session.commit()

            assert first.id is not None
            assert second.id == first.id + 1

    async def test_every_table_can_take_a_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            guest = Guest(vmid=101, guest_type="vm", name="docker-host", node="pve")
            session.add(guest)
            await session.commit()
            assert guest.id is not None


class TestConstraints:
    async def test_enum_check_constraint_is_enforced(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(User(username="bad", password_hash="x", role="superuser"))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_username_is_unique(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            session.add(User(username="dup", password_hash="x", role="viewer"))
            await session.commit()
        async with session_factory() as session:
            session.add(User(username="dup", password_hash="y", role="viewer"))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_foreign_keys_are_enforced(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """SQLite only enforces foreign keys when the pragma is on for that connection."""
        async with session_factory() as session:
            session.add(
                RefreshToken(
                    user_id=999999,
                    token_hash="a" * 64,
                    family_id="fam",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    created_at=datetime.now(UTC),
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_backup_filename_is_unique_per_storage(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        def build() -> BackupHistory:
            return BackupHistory(
                vmid=101,
                guest_type="vm",
                guest_name="docker-host",
                node="pve",
                storage="backup-hdd",
                filename="vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
            )

        async with session_factory() as session:
            session.add(build())
            await session.commit()
        async with session_factory() as session:
            session.add(build())
            with pytest.raises(IntegrityError):
                await session.commit()


class TestTimestamps:
    async def test_naive_datetimes_are_refused(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A naive datetime is a silent hour-offset bug months later; reject it on write."""
        async with session_factory() as session:
            session.add(
                RefreshToken(
                    user_id=1,
                    token_hash="b" * 64,
                    family_id="fam",
                    expires_at=datetime(2030, 1, 1),  # noqa: DTZ001 - deliberately naive
                    created_at=datetime.now(UTC),
                )
            )
            with pytest.raises((StatementError, ValueError)):
                await session.commit()

    async def test_timestamps_round_trip_as_utc(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from zoneinfo import ZoneInfo

        jakarta = datetime(2026, 7, 19, 1, 0, 4, tzinfo=ZoneInfo("Asia/Jakarta"))
        async with session_factory() as session:
            user = User(username="tz", password_hash="x", role="viewer", last_login_at=jakarta)
            session.add(user)
            await session.commit()
            user_id = user.id

        async with session_factory() as session:
            loaded = await session.get(User, user_id)
            assert loaded is not None
            assert loaded.last_login_at is not None
            assert loaded.last_login_at.tzinfo is not None
            assert loaded.last_login_at == jakarta  # same instant
            assert loaded.last_login_at.utcoffset() == timedelta(0)  # stored as UTC


class TestUserRepository:
    async def test_usernames_are_stored_case_folded(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            repository = SqlAlchemyUserRepository(session)
            await repository.create(
                username="  MixedCase  ", password_hash="x", role=UserRole.VIEWER
            )
            await session.commit()

            assert await repository.get_by_username("mixedcase") is not None
            assert await repository.get_by_username("MIXEDCASE") is not None

    def test_normalisation_uses_casefold(self) -> None:
        assert normalise_username("  ADMIN ") == "admin"
        assert normalise_username("STRASSE") == normalise_username("strasse")

    async def test_count_reflects_inserts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            repository = SqlAlchemyUserRepository(session)
            assert await repository.count() == 0
            await repository.create(username="a", password_hash="x", role=UserRole.ADMIN)
            await session.commit()
            assert await repository.count() == 1


class TestSettingsRepository:
    async def test_upsert_replaces_rather_than_duplicates(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            repository = SqlAlchemySettingsRepository(session)
            await repository.upsert(
                section=SettingsSection.GENERAL,
                key="timezone",
                value="Asia/Jakarta",
                value_type=SettingValueType.STRING,
            )
            await repository.upsert(
                section=SettingsSection.GENERAL,
                key="timezone",
                value="UTC",
                value_type=SettingValueType.STRING,
            )
            await session.commit()

            rows = (await session.execute(select(Setting))).scalars().all()
            assert len(rows) == 1
            assert unwrap(rows[0].value) == "UTC"

    async def test_values_are_wrapped_for_json_validity(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            repository = SqlAlchemySettingsRepository(session)
            row = await repository.upsert(
                section=SettingsSection.RETENTION,
                key="keep_local",
                value=2,
                value_type=SettingValueType.INT,
            )
            assert row.value == {"v": 2}
            assert unwrap(row.value) == 2


class TestPortability:
    """Guards the rules that make a PostgreSQL migration mechanical."""

    def test_every_constraint_is_named(self) -> None:
        unnamed: list[str] = []
        for table in Base.metadata.tables.values():
            for constraint in table.constraints:
                if constraint.name is None:
                    unnamed.append(f"{table.name}.{type(constraint).__name__}")
            for index in table.indexes:
                if index.name is None:
                    unnamed.append(f"{table.name}.Index")
        assert unnamed == [], f"unnamed constraints break SQLite batch migrations: {unnamed}"

    def test_no_native_enum_columns(self) -> None:
        from sqlalchemy import Enum

        native = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, Enum)
        ]
        assert native == [], f"native ENUM types are not portable: {native}"

    def test_every_datetime_column_is_utc_aware(self) -> None:
        from app.db.types import UTCDateTime

        wrong = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if column.type.__class__.__name__ == "DateTime"
            and not isinstance(column.type, UTCDateTime)
        ]
        assert wrong == [], f"columns bypassing UTCDateTime: {wrong}"

    async def test_sqlite_pragmas_are_applied(self, settings: Settings) -> None:
        from sqlalchemy import text

        from app.db.session import Database

        database = Database(settings)
        async with database.engine.connect() as connection:
            foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
            journal = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
        await database.dispose()

        assert foreign_keys == 1
        assert str(journal).lower() == "wal"

    async def test_schema_matches_the_models(self, settings: Settings) -> None:
        """`create_all` and the ORM must agree on every table name."""
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        await engine.dispose()

        assert set(Base.metadata.tables) <= set(names)
