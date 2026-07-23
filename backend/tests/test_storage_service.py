"""Storage-service live composition, attribution and forecast mathematics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.backup import BackupHistory
from app.repositories.storage_snapshot_repository import (
    GuestStorageRecord,
    NewStorageSnapshot,
    SqlAlchemyStorageSnapshotRepository,
    StorageSnapshotRecord,
)
from app.schemas.agent import AgentRemoteQuota, AgentStorageStatus
from app.schemas.enums import SettingsSection
from app.schemas.settings import GDriveSettings, RetentionSettings, SectionModel
from app.schemas.storage import StorageSeverity
from app.services.storage_service import StorageService, effective_used_percent, linear_forecast

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def sample(
    day: float,
    used: int,
    *,
    free: int | None = None,
    total: int = 1_000,
    sample_id: int = 1,
) -> StorageSnapshotRecord:
    return StorageSnapshotRecord(
        id=sample_id,
        captured_at=NOW + timedelta(days=day),
        local_total_bytes=total,
        local_used_bytes=used,
        local_free_bytes=total - used if free is None else free,
        remote_used_bytes=None,
        remote_quota_bytes=None,
        backup_count=2,
        oldest_backup_at=None,
    )


class FakeSnapshots:
    def __init__(
        self,
        *,
        rows: list[StorageSnapshotRecord] | None = None,
        guests: list[GuestStorageRecord] | None = None,
    ) -> None:
        self.rows = rows or []
        self.guests = guests or []

    async def create(self, new: NewStorageSnapshot) -> StorageSnapshotRecord:
        del new
        raise AssertionError("not used by StorageService")

    async def latest(self) -> StorageSnapshotRecord | None:
        return self.rows[-1] if self.rows else None

    async def since(
        self, *, cutoff: datetime, until: datetime | None = None
    ) -> list[StorageSnapshotRecord]:
        return [
            row
            for row in self.rows
            if row.captured_at >= cutoff and (until is None or row.captured_at <= until)
        ]

    async def oldest_local_backup_at(self) -> datetime | None:
        return None

    async def by_guest(self) -> list[GuestStorageRecord]:
        return self.guests


class FakeSettings:
    def __init__(self, *, gdrive: GDriveSettings | None = None) -> None:
        self.gdrive = gdrive or GDriveSettings()
        self.retention = RetentionSettings()

    async def get_section(self, section: SettingsSection) -> SectionModel:
        if section is SettingsSection.GDRIVE:
            return self.gdrive
        if section is SettingsSection.RETENTION:
            return self.retention
        raise AssertionError(section)


class FakeAgent:
    def __init__(self) -> None:
        self.local_error: Exception | None = None
        self.remote_error: Exception | None = None
        self.remote_calls = 0

    async def storage_status(self) -> AgentStorageStatus:
        if self.local_error is not None:
            raise self.local_error
        return AgentStorageStatus.model_validate(
            {
                "dump_root": {
                    "path": "/mnt/backup-hdd/dump",
                    "total_bytes": 1_000,
                    # Deliberately inconsistent with free: the service must derive its percent
                    # from available space rather than trusting this reserved-block-sensitive pair.
                    "used_bytes": 600,
                    "free_bytes": 350,
                    "used_percent": 60,
                },
                "storages": [
                    {
                        "name": "backup-hdd",
                        "type": "dir",
                        "active": True,
                        "total_bytes": 1_000,
                        "used_bytes": 600,
                        "available_bytes": 350,
                        "used_percent": 60,
                    }
                ],
                "artifact_count": 4,
                "artifact_bytes": 500,
            }
        )

    async def remote_quota(self, *, remote: str) -> AgentRemoteQuota:
        assert remote == "gdrive"
        self.remote_calls += 1
        if self.remote_error is not None:
            raise self.remote_error
        return AgentRemoteQuota(
            remote=remote,
            total_bytes=10_000,
            used_bytes=2_000,
            free_bytes=8_000,
            used_percent=20,
        )


def service(
    *,
    agent: FakeAgent | None = None,
    settings: FakeSettings | None = None,
    snapshots: FakeSnapshots | None = None,
) -> StorageService:
    return StorageService(
        snapshots=snapshots or FakeSnapshots(),
        settings_service=settings or FakeSettings(),
        agent=agent or FakeAgent(),
    )


class TestLiveStatus:
    async def test_effective_percent_drives_thresholds(self) -> None:
        result = await service().live(now=NOW)

        assert result.local is not None
        assert result.local.used_percent == 65.0
        assert result.local.severity is StorageSeverity.HEALTHY
        assert result.local.artifact_count == 4
        assert result.storages[0].name == "backup-hdd"

    async def test_local_failure_does_not_hide_a_working_remote(self) -> None:
        agent = FakeAgent()
        agent.local_error = RuntimeError("statvfs unavailable")
        settings = FakeSettings(gdrive=GDriveSettings(enabled=True))

        result = await service(agent=agent, settings=settings).live(now=NOW)

        assert result.local is None
        assert result.local_detail == "statvfs unavailable"
        assert result.remote.used_bytes == 2_000
        assert result.remote.detail is None

    async def test_remote_failure_never_looks_like_zero_usage(self) -> None:
        agent = FakeAgent()
        agent.remote_error = RuntimeError("quota endpoint unavailable")
        settings = FakeSettings(gdrive=GDriveSettings(enabled=True))

        result = await service(agent=agent, settings=settings).live(now=NOW)

        assert result.local is not None
        assert result.remote.configured is True
        assert result.remote.used_bytes is None
        assert result.remote.quota_bytes is None
        assert result.remote.detail == "quota endpoint unavailable"

    async def test_disabled_remote_is_not_contacted(self) -> None:
        agent = FakeAgent()
        result = await service(agent=agent).live(now=NOW)

        assert result.remote.configured is False
        assert result.remote.used_bytes is None
        assert agent.remote_calls == 0


class TestForecast:
    def test_linear_growth_uses_latest_free_space(self) -> None:
        result = linear_forecast([sample(0, 100), sample(1, 200), sample(2, 300)])

        assert result.growth_bytes_per_day == pytest.approx(100)
        assert result.days_until_full == pytest.approx(7)
        assert result.estimated_full_at == NOW + timedelta(days=9)

    def test_irregular_intervals_are_a_real_least_squares_fit(self) -> None:
        result = linear_forecast([sample(0, 100), sample(0.5, 150), sample(3, 400)])
        assert result.growth_bytes_per_day == pytest.approx(100)

    @pytest.mark.parametrize(
        "rows",
        [[], [sample(0, 100)], [sample(0, 100), sample(0, 200, sample_id=2)]],
    )
    def test_insufficient_data_is_truthful(self, rows: list[StorageSnapshotRecord]) -> None:
        result = linear_forecast(rows)
        assert result.days_until_full is None
        assert result.estimated_full_at is None
        assert result.detail is not None

    def test_stable_or_declining_usage_has_no_fill_date(self) -> None:
        result = linear_forecast([sample(0, 300), sample(1, 300), sample(2, 200)])
        assert result.growth_bytes_per_day is not None
        assert result.growth_bytes_per_day < 0
        assert result.days_until_full is None
        assert "declining" in (result.detail or "")

    def test_full_storage_is_reported_without_dividing_by_a_slope(self) -> None:
        result = linear_forecast([sample(0, 1_000, free=0)])
        assert result.days_until_full == 0
        assert result.estimated_full_at == NOW

    def test_tiny_positive_slope_cannot_overflow_the_fill_date(self) -> None:
        enormous = 10**18
        result = linear_forecast([sample(0, 1, total=enormous), sample(30, 2, total=enormous)])

        assert result.growth_bytes_per_day is not None
        assert result.growth_bytes_per_day > 0
        assert result.days_until_full is None
        assert result.estimated_full_at is None
        assert "representable" in (result.detail or "")

    async def test_service_uses_only_the_trailing_thirty_days(self) -> None:
        snapshots = FakeSnapshots(rows=[sample(-31, 900), sample(-1, 100), sample(0, 200)])
        result = await service(snapshots=snapshots).forecast(now=NOW)
        assert result.sample_count == 2
        assert result.growth_bytes_per_day == pytest.approx(100)


class TestByGuest:
    async def test_maps_grouped_totals(self) -> None:
        snapshots = FakeSnapshots(
            guests=[
                GuestStorageRecord(108, "lxc", "nextcloud", 2, 800, NOW),
                GuestStorageRecord(101, "vm", "docker", 2, 200, NOW - timedelta(days=1)),
            ]
        )

        result = await service(snapshots=snapshots).by_guest()

        assert [item.vmid for item in result.items] == [108, 101]
        assert result.items[0].guest_type.value == "lxc"
        assert result.total_bytes == 1_000


def test_effective_used_percent_uses_available_space() -> None:
    assert effective_used_percent(1_000, 350) == 65.0
    assert effective_used_percent(0, 0) is None
    assert effective_used_percent(1_000, 2_000) == 0.0
    assert effective_used_percent(1_000, -1) == 100.0


class TestStorageRepository:
    async def test_by_guest_uses_identity_latest_name_and_only_live_successes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        def backup(
            *,
            vmid: int,
            guest_type: str,
            name: str,
            size: int | None,
            day: int,
            status: str = "success",
            deleted: bool = False,
        ) -> BackupHistory:
            filename = f"vzdump-{guest_type}-{vmid}-{abs(day)}.backup"
            return BackupHistory(
                vmid=vmid,
                guest_type=guest_type,
                guest_name=name,
                node="pve",
                storage="backup-hdd",
                filename=filename,
                local_path=f"/mnt/backup-hdd/dump/{filename}",
                size_bytes=size,
                status=status,
                started_at=NOW + timedelta(days=day),
                local_deleted_at=NOW if deleted else None,
            )

        async with session_factory() as session:
            session.add_all(
                [
                    backup(vmid=101, guest_type="vm", name="old-name", size=100, day=-2),
                    backup(vmid=101, guest_type="vm", name="new-name", size=200, day=-1),
                    # Same numeric id, different kind: never merge these identities.
                    backup(vmid=101, guest_type="lxc", name="container", size=900, day=-1),
                    backup(
                        vmid=102,
                        guest_type="vm",
                        name="failed",
                        size=5_000,
                        day=-1,
                        status="failed",
                    ),
                    backup(
                        vmid=103, guest_type="vm", name="deleted", size=5_000, day=-1, deleted=True
                    ),
                    backup(vmid=104, guest_type="vm", name="unknown-size", size=None, day=-1),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            rows = await SqlAlchemyStorageSnapshotRepository(session).by_guest()

        assert [(row.vmid, row.guest_type) for row in rows] == [(101, "lxc"), (101, "vm")]
        assert rows[0].total_bytes == 900
        assert rows[1].total_bytes == 300
        assert rows[1].backup_count == 2
        assert rows[1].guest_name == "new-name"
