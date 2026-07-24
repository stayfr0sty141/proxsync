"""Periodic storage sampling and threshold transitions.

The network is sampled before a database transaction is opened.  A slow or unavailable agent
therefore never holds SQLite's single writer or a PostgreSQL connection while retry/backoff runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.events import EVENT_STORAGE_UPDATE, EventBus
from app.core.logging import logger, set_correlation_id
from app.db.models.backup import BackupHistory
from app.db.session import Database
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.storage_snapshot_repository import (
    NewStorageSnapshot,
    SqlAlchemyStorageSnapshotRepository,
    StorageSnapshotRecord,
)
from app.schemas.agent import AgentRemoteQuota
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    NotificationEvent,
    SettingsSection,
    UploadStatus,
)
from app.schemas.settings import GDriveSettings, ProxmoxSettings, RetentionSettings
from app.schemas.storage import StorageSeverity
from app.services.notification_service import NotificationGateway
from app.services.settings_service import SettingsService
from app.services.storage_service import (
    StorageAgent,
    classify_storage,
    effective_used_percent,
    severity_increased,
)


class StorageSampler:
    def __init__(
        self,
        *,
        database: Database,
        agent: StorageAgent,
        events: EventBus,
        settings: Settings,
        secret_box: SecretBox,
        notifications: NotificationGateway | None = None,
    ) -> None:
        self._database = database
        self._agent = agent
        self._events = events
        self._settings = settings
        self._secret_box = secret_box
        self._notifications = notifications

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._sample_lock = asyncio.Lock()
        self._last_sample_at: datetime | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="proxsync-storage-sampler")
        logger.info(
            "storage_sampler_started",
            interval_seconds=self._settings.storage_sample_interval_seconds,
        )

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            # Sampling is entirely repeatable. Cancelling an in-flight GET or transaction is
            # safer than making shutdown wait for the agent client's full retry window; the DB
            # context rolls an interrupted write back.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("storage_sampler_stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_sample_at(self) -> datetime | None:
        return self._last_sample_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.sample_once()
            except Exception as exc:  # noqa: BLE001 - one bad tick must not stop monitoring
                self._last_error = str(exc)
                logger.error("storage_sample_failed", error=str(exc), exc_info=True)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._settings.storage_sample_interval_seconds,
                )

    async def sample_once(self, *, now: datetime | None = None) -> StorageSnapshotRecord | None:
        """Capture one sample; concurrent callers queue rather than overlap agent reads."""

        async with self._sample_lock:
            # Background tasks must not inherit the correlation id of whichever request happened
            # to run on this context before the task was spawned.
            set_correlation_id(None)
            captured_at = now or datetime.now(UTC)
            gdrive, retention = await self._preferences()

            # No database session is alive across either request.
            try:
                local = await self._agent.storage_status()
                await self._sync_disk_artifacts()
            except Exception as exc:  # noqa: BLE001 - a missing sample is safer than fake zeros
                self._last_error = str(getattr(exc, "detail", None) or exc)
                logger.warning("storage_local_sample_unavailable", error=self._last_error)
                return None

            remote: AgentRemoteQuota | None = None
            remote_error: str | None = None
            if gdrive.enabled:
                try:
                    remote = await self._agent.remote_quota(remote=gdrive.remote_name)
                except Exception as exc:  # noqa: BLE001 - persist the valid local half
                    remote_error = str(getattr(exc, "detail", None) or exc)
                    logger.warning(
                        "storage_remote_sample_unavailable",
                        remote=gdrive.remote_name,
                        error=remote_error,
                    )

            usage = local.dump_root
            used_percent = effective_used_percent(usage.total_bytes, usage.free_bytes)
            severity = classify_storage(
                used_percent,
                warning_percent=retention.storage_warning_percent,
                critical_percent=retention.storage_critical_percent,
            )

            async with self._database.session() as session:
                snapshots = SqlAlchemyStorageSnapshotRepository(session)
                previous = await snapshots.latest()
                previous_severity = self._previous_severity(previous, retention=retention)
                oldest = await snapshots.oldest_local_backup_at()
                saved = await snapshots.create(
                    NewStorageSnapshot(
                        captured_at=captured_at,
                        local_total_bytes=usage.total_bytes,
                        local_used_bytes=usage.used_bytes,
                        local_free_bytes=usage.free_bytes,
                        remote_used_bytes=remote.used_bytes if remote is not None else None,
                        remote_quota_bytes=remote.total_bytes if remote is not None else None,
                        backup_count=local.artifact_count,
                        oldest_backup_at=oldest,
                    )
                )
                alert = severity_increased(previous_severity, severity)
                if alert and self._notifications is not None:
                    # Keyed by the severity being entered, and windowed: a disk hovering on the
                    # warning line must not send a message every fifteen minutes, but crossing
                    # into critical later is genuinely new and gets through.
                    await self._notifications.emit(
                        session,
                        NotificationEvent.STORAGE_THRESHOLD,
                        dedupe_key=f"storage:{severity.value}",
                        variables={
                            "severity": severity.value,
                            "previous_severity": previous_severity.value,
                            "used_bytes": usage.used_bytes,
                            "total_bytes": usage.total_bytes,
                            "free_bytes": usage.free_bytes,
                            "used_percent": used_percent,
                        },
                    )

            # `Database.session()` has committed before anything tells readers to refetch.
            self._last_sample_at = captured_at
            self._last_error = remote_error
            if alert:
                logger.warning(
                    "storage_threshold_crossed",
                    previous=previous_severity.value,
                    severity=severity.value,
                    used_percent=used_percent,
                    warning_percent=retention.storage_warning_percent,
                    critical_percent=retention.storage_critical_percent,
                )
            self._events.publish(
                EVENT_STORAGE_UPDATE,
                snapshot_id=saved.id,
                captured_at=saved.captured_at,
                total_bytes=saved.local_total_bytes,
                used_bytes=saved.local_used_bytes,
                free_bytes=saved.local_free_bytes,
                used_percent=used_percent,
                severity=severity.value,
                alert=alert,
                remote_available=remote is not None,
                remote_detail=remote_error,
            )
            return saved

    async def _preferences(self) -> tuple[GDriveSettings, RetentionSettings]:
        async with self._database.session() as session:
            service = SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
            )
            gdrive = await service.get_section(SettingsSection.GDRIVE)
            retention = await service.get_section(SettingsSection.RETENTION)
        assert isinstance(gdrive, GDriveSettings)
        assert isinstance(retention, RetentionSettings)
        return gdrive, retention

    @staticmethod
    def _previous_severity(
        previous: StorageSnapshotRecord | None, *, retention: RetentionSettings
    ) -> StorageSeverity:
        if previous is None:
            return StorageSeverity.HEALTHY
        percent = effective_used_percent(previous.local_total_bytes, previous.local_free_bytes)
        return classify_storage(
            percent,
            warning_percent=retention.storage_warning_percent,
            critical_percent=retention.storage_critical_percent,
        )

    async def _sync_disk_artifacts(self) -> None:
        list_fn = getattr(self._agent, "list_artifacts", None)
        if not callable(list_fn):
            return
        try:
            raw_artifacts = list_fn()
            artifacts: list[Any] = (
                await raw_artifacts if asyncio.iscoroutine(raw_artifacts) else raw_artifacts
            )
            async with self._database.session() as session:
                history_repo = SqlAlchemyBackupHistoryRepository(session)
                guest_repo = SqlAlchemyGuestRepository(session)
                settings_svc = SettingsService(
                    repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
                )
                pve_section = await settings_svc.get_section(SettingsSection.PROXMOX)
                if not isinstance(pve_section, ProxmoxSettings):
                    return
                node_name = pve_section.node or "server"

                existing = {
                    item.filename
                    for item in await history_repo.search(limit=10000)
                    if item.filename
                }
                all_guests = {(g.vmid, g.guest_type): g for g in await guest_repo.list_all()}

                for art in artifacts:
                    if art.filename not in existing:
                        guest_obj = all_guests.get((art.vmid, art.guest_type.value))
                        guest_name = (
                            guest_obj.name
                            if guest_obj
                            else f"{art.guest_type.value.upper()} {art.vmid}"
                        )
                        guest_id = guest_obj.id if guest_obj else None
                        comp_val = (
                            art.compression.value
                            if hasattr(art.compression, "value")
                            else str(art.compression)
                        )
                        rec = BackupHistory(
                            vmid=art.vmid,
                            guest_type=art.guest_type.value,
                            guest_name=guest_name,
                            node=node_name,
                            storage="hdd_3tb",
                            filename=art.filename,
                            local_path=str(art.path),
                            size_bytes=art.size_bytes,
                            mode=BackupMode.SNAPSHOT.value,
                            compression=comp_val,
                            checksum_sha256=getattr(art, "checksum_sha256", None),
                            status=BackupStatus.SUCCESS.value,
                            upload_status=UploadStatus.NOT_REQUIRED.value,
                            started_at=art.created_at,
                            finished_at=art.created_at,
                            guest_id=guest_id,
                        )
                        session.add(rec)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("storage_disk_artifacts_sync_failed", error=str(exc))
