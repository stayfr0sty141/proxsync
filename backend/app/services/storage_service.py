"""Storage status, history, forecasting and per-guest attribution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.repositories.storage_snapshot_repository import (
    GuestStorageRecord,
    StorageSnapshotRecord,
    StorageSnapshotRepository,
)
from app.schemas.agent import AgentRemoteQuota, AgentStorageStatus
from app.schemas.enums import GuestType, SettingsSection
from app.schemas.settings import GDriveSettings, RetentionSettings, SectionModel
from app.schemas.storage import (
    GuestStorageResponse,
    LocalStorageResponse,
    RemoteStorageResponse,
    StorageByGuestResponse,
    StorageEntryResponse,
    StorageForecastResponse,
    StorageHistoryResponse,
    StorageSeverity,
    StorageSnapshotResponse,
    StorageStatusResponse,
)

FORECAST_WINDOW_DAYS = 30


class StorageAgent(Protocol):
    async def storage_status(self) -> AgentStorageStatus: ...
    async def remote_quota(self, *, remote: str) -> AgentRemoteQuota: ...


class SettingsReader(Protocol):
    async def get_section(self, section: SettingsSection) -> SectionModel: ...


class StorageService:
    def __init__(
        self,
        *,
        snapshots: StorageSnapshotRepository,
        settings_service: SettingsReader,
        agent: StorageAgent,
    ) -> None:
        self._snapshots = snapshots
        self._settings_service = settings_service
        self._agent = agent

    async def live(self, *, now: datetime | None = None) -> StorageStatusResponse:
        captured_at = now or datetime.now(UTC)
        retention = await self._retention_settings()
        gdrive = await self._gdrive_settings()

        local_status: AgentStorageStatus | None = None
        local_detail: str | None = None
        try:
            local_status = await self._agent.storage_status()
        except Exception as exc:  # noqa: BLE001 - this endpoint returns an honest partial view
            local_detail = _error_detail(exc)

        remote_quota: AgentRemoteQuota | None = None
        remote_detail: str | None = None
        if gdrive.enabled:
            try:
                remote_quota = await self._agent.remote_quota(remote=gdrive.remote_name)
            except Exception as exc:  # noqa: BLE001 - local figures remain useful
                remote_detail = _error_detail(exc)

        local: LocalStorageResponse | None = None
        storages: list[StorageEntryResponse] = []
        if local_status is not None:
            usage = local_status.dump_root
            percent = effective_used_percent(usage.total_bytes, usage.free_bytes)
            local = LocalStorageResponse(
                path=usage.path,
                total_bytes=usage.total_bytes,
                used_bytes=usage.used_bytes,
                free_bytes=usage.free_bytes,
                used_percent=percent,
                artifact_count=local_status.artifact_count,
                artifact_bytes=local_status.artifact_bytes,
                severity=classify_storage(
                    percent,
                    warning_percent=retention.storage_warning_percent,
                    critical_percent=retention.storage_critical_percent,
                ),
                warning_percent=retention.storage_warning_percent,
                critical_percent=retention.storage_critical_percent,
            )
            storages = [
                StorageEntryResponse(
                    name=entry.name,
                    type=entry.type,
                    active=entry.active,
                    total_bytes=entry.total_bytes,
                    used_bytes=entry.used_bytes,
                    available_bytes=entry.available_bytes,
                    used_percent=entry.used_percent,
                )
                for entry in local_status.storages
            ]

        remote = _remote_response(gdrive, remote_quota, detail=remote_detail)
        return StorageStatusResponse(
            captured_at=captured_at,
            local=local,
            local_detail=local_detail,
            storages=storages,
            remote=remote,
        )

    async def history(
        self, *, days: int = FORECAST_WINDOW_DAYS, now: datetime | None = None
    ) -> StorageHistoryResponse:
        current = now or datetime.now(UTC)
        retention = await self._retention_settings()
        rows = await self._snapshots.since(cutoff=current - timedelta(days=days), until=current)
        return StorageHistoryResponse(
            days=days,
            items=[self._snapshot_response(row, retention=retention) for row in rows],
        )

    async def forecast(self, *, now: datetime | None = None) -> StorageForecastResponse:
        current = now or datetime.now(UTC)
        samples = await self._snapshots.since(
            cutoff=current - timedelta(days=FORECAST_WINDOW_DAYS), until=current
        )
        return linear_forecast(samples)

    async def by_guest(self) -> StorageByGuestResponse:
        rows = await self._snapshots.by_guest()
        items = [_guest_response(row) for row in rows]
        return StorageByGuestResponse(
            items=items,
            total_bytes=sum(item.total_bytes for item in items),
        )

    async def _retention_settings(self) -> RetentionSettings:
        section = await self._settings_service.get_section(SettingsSection.RETENTION)
        assert isinstance(section, RetentionSettings)
        return section

    async def _gdrive_settings(self) -> GDriveSettings:
        section = await self._settings_service.get_section(SettingsSection.GDRIVE)
        assert isinstance(section, GDriveSettings)
        return section

    @staticmethod
    def _snapshot_response(
        row: StorageSnapshotRecord, *, retention: RetentionSettings
    ) -> StorageSnapshotResponse:
        percent = effective_used_percent(row.local_total_bytes, row.local_free_bytes)
        return StorageSnapshotResponse(
            id=row.id,
            captured_at=row.captured_at,
            local_total_bytes=row.local_total_bytes,
            local_used_bytes=row.local_used_bytes,
            local_free_bytes=row.local_free_bytes,
            local_used_percent=percent,
            severity=classify_storage(
                percent,
                warning_percent=retention.storage_warning_percent,
                critical_percent=retention.storage_critical_percent,
            ),
            remote_used_bytes=row.remote_used_bytes,
            remote_quota_bytes=row.remote_quota_bytes,
            backup_count=row.backup_count,
            oldest_backup_at=row.oldest_backup_at,
        )


def effective_used_percent(total_bytes: int, free_bytes: int) -> float | None:
    """Percentage of capacity unavailable to the backup process.

    The agent reports ``used`` from ``f_bfree`` but ``free`` from ``f_bavail``.  Reserved
    filesystem blocks therefore make ``used + free != total``; deriving the percentage from
    free space is conservative and consistent with how much room a new backup can rely on.
    """

    if total_bytes <= 0:
        return None
    percent = (total_bytes - free_bytes) / total_bytes * 100
    return round(min(max(percent, 0.0), 100.0), 2)


def classify_storage(
    used_percent: float | None, *, warning_percent: int, critical_percent: int
) -> StorageSeverity:
    if used_percent is None:
        return StorageSeverity.UNKNOWN
    if used_percent >= critical_percent:
        return StorageSeverity.CRITICAL
    if used_percent >= warning_percent:
        return StorageSeverity.WARNING
    return StorageSeverity.HEALTHY


def severity_increased(previous: StorageSeverity, current: StorageSeverity) -> bool:
    rank = {
        StorageSeverity.UNKNOWN: -1,
        StorageSeverity.HEALTHY: 0,
        StorageSeverity.WARNING: 1,
        StorageSeverity.CRITICAL: 2,
    }
    return rank[current] > rank[previous] and current in {
        StorageSeverity.WARNING,
        StorageSeverity.CRITICAL,
    }


def linear_forecast(samples: list[StorageSnapshotRecord]) -> StorageForecastResponse:
    """Least-squares local growth over the trailing window.

    ``local_free_bytes / slope`` is intentional.  It remains correct on filesystems with
    reserved blocks, where total minus the agent's used figure is not the space available to a
    future backup.
    """

    common = {
        "sample_count": len(samples),
        "window_started_at": samples[0].captured_at if samples else None,
        "window_ended_at": samples[-1].captured_at if samples else None,
    }
    if not samples:
        return StorageForecastResponse(**common, detail="No storage samples are available yet")

    latest = samples[-1]
    if latest.local_free_bytes <= 0:
        return StorageForecastResponse(
            **common,
            days_until_full=0.0,
            estimated_full_at=latest.captured_at,
            detail="Storage is already full",
        )

    if len(samples) < 2:
        return StorageForecastResponse(
            **common, detail="At least two storage samples are required for a forecast"
        )

    origin = samples[0].captured_at
    x = [(sample.captured_at - origin).total_seconds() / 86_400 for sample in samples]
    y = [float(sample.local_used_bytes) for sample in samples]
    x_mean = sum(x) / len(x)
    y_mean = sum(y) / len(y)
    denominator = sum((value - x_mean) ** 2 for value in x)
    if denominator == 0:
        return StorageForecastResponse(
            **common, detail="Storage samples need distinct timestamps for a forecast"
        )

    slope = sum((xv - x_mean) * (yv - y_mean) for xv, yv in zip(x, y, strict=True)) / denominator
    if slope <= 0:
        return StorageForecastResponse(
            **common,
            growth_bytes_per_day=slope,
            detail="Storage usage is stable or declining",
        )

    days = latest.local_free_bytes / slope
    try:
        estimated_full_at = latest.captured_at + timedelta(days=days)
    except (OverflowError, ValueError):
        # A near-flat positive fit can put the mathematical date beyond datetime.max. That
        # means "no useful fill date", not a 500 from a read-only dashboard endpoint.
        return StorageForecastResponse(
            **common,
            growth_bytes_per_day=slope,
            detail="Growth is positive but too small for a representable fill date",
        )
    return StorageForecastResponse(
        **common,
        growth_bytes_per_day=slope,
        days_until_full=days,
        estimated_full_at=estimated_full_at,
    )


def _remote_response(
    settings: GDriveSettings,
    quota: AgentRemoteQuota | None,
    *,
    detail: str | None,
) -> RemoteStorageResponse:
    if not settings.enabled:
        return RemoteStorageResponse(
            remote=settings.remote_name,
            configured=False,
            detail="Google Drive sync is disabled",
        )
    if quota is None:
        return RemoteStorageResponse(
            remote=settings.remote_name,
            configured=True,
            detail=detail or "Remote quota is unavailable",
        )
    return RemoteStorageResponse(
        remote=settings.remote_name,
        configured=True,
        quota_bytes=quota.total_bytes,
        used_bytes=quota.used_bytes,
        free_bytes=quota.free_bytes,
        trashed_bytes=quota.trashed_bytes,
        used_percent=quota.used_percent,
        detail=detail,
    )


def _guest_response(row: GuestStorageRecord) -> GuestStorageResponse:
    return GuestStorageResponse(
        vmid=row.vmid,
        guest_type=GuestType(row.guest_type),
        guest_name=row.guest_name,
        backup_count=row.backup_count,
        total_bytes=row.total_bytes,
        latest_backup_at=row.latest_backup_at,
    )


def _error_detail(exc: Exception) -> str:
    return str(getattr(exc, "detail", None) or exc)
