"""Backup orchestration: what to back up, with which options, and what happened.

This service decides and records. It never *runs* a backup — that is the run executor's job
(`app.workers.backup_runner`). Keeping the decision and the execution apart is what lets a
run survive a restart: the intent is durable in the database before anything is started, so
recovery is a query rather than a guess. Short, synchronous operations that must answer
within the request, such as deleting an artifact, do call the agent directly.

**The allow-list is enforced here, for manual backups too.** `Guest.backup_enabled` is
described as the gate every job must pass, and a "Backup Now" that ignored it would make the
gate decorative — an operator could back up a guest that policy says must never be copied to
Google Drive. The refusal names the guest and the fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.clients.agent_client import AgentClient
from app.core.errors import ConfigurationError, Conflict, NotFound, ValidationFailed
from app.core.logging import get_correlation_id, logger
from app.db.models.backup import BackupHistory, BackupJob, BackupRun
from app.db.models.guest import Guest
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.schemas.agent import AgentDeletedArtifact
from app.schemas.backup import (
    GuestTarget,
    ManualBackupRequest,
    ResolvedTarget,
    RunOptions,
)
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestType,
    RunStatus,
    SettingsSection,
    TargetSelector,
    TriggerType,
    UploadStatus,
)
from app.schemas.settings import AgentSettings, GDriveSettings, RetentionSettings
from app.services.settings_service import SettingsService


class TargetResolution:
    """The outcome of turning a selector into a concrete guest list.

    `skipped` is not an error list — a job that excludes a disabled guest is behaving
    correctly — but it must be visible, because a schedule that resolves to nothing looks
    identical to a healthy one from the outside.
    """

    def __init__(self, targets: list[ResolvedTarget], skipped: list[str]) -> None:
        self.targets = targets
        self.skipped = skipped


class BackupService:
    def __init__(
        self,
        *,
        guests: SqlAlchemyGuestRepository,
        runs: SqlAlchemyBackupRunRepository,
        history: SqlAlchemyBackupHistoryRepository,
        settings_service: SettingsService,
        agent: AgentClient | None = None,
        restores: SqlAlchemyRestoreRepository | None = None,
    ) -> None:
        self._guests = guests
        self._runs = runs
        self._history = history
        self._settings_service = settings_service
        self._agent = agent
        self._restores = restores

    # ---- target resolution ---------------------------------------------------

    async def resolve_job_targets(self, job: BackupJob) -> TargetResolution:
        enabled = {
            (guest.vmid, guest.guest_type): guest for guest in await self._guests.enabled_guests()
        }
        selector = TargetSelector(job.target_selector)

        if selector is TargetSelector.ALL:
            return TargetResolution([_resolve(guest) for guest in enabled.values()], [])

        named = [(target.vmid, target.guest_type) for target in job.targets]

        if selector is TargetSelector.EXCLUDE:
            excluded = set(named)
            kept = [guest for key, guest in enabled.items() if key not in excluded]
            return TargetResolution([_resolve(guest) for guest in kept], [])

        targets: list[ResolvedTarget] = []
        skipped: list[str] = []
        for key in named:
            guest = enabled.get(key)
            if guest is None:
                skipped.append(await self._skip_reason(key))
            else:
                targets.append(_resolve(guest))
        return TargetResolution(targets, skipped)

    async def resolve_explicit_targets(self, requested: list[GuestTarget]) -> list[ResolvedTarget]:
        """Manual selection. Every named guest must exist and be enabled, or the whole request
        is refused — a partial backup that quietly dropped a guest would be worse than a 400.
        """
        resolved: list[ResolvedTarget] = []
        problems: list[str] = []

        for target in requested:
            guest = await self._guests.get_by_vmid(target.vmid, target.guest_type)
            if guest is None:
                problems.append(
                    f"{target.guest_type.value} {target.vmid} is not in the inventory; "
                    "refresh guests, or check that it exists on the node"
                )
            elif not guest.backup_enabled:
                problems.append(
                    f"{target.guest_type.value} {target.vmid} ({guest.name}) is not enabled for "
                    "backup; enable it on the Guests page first"
                )
            else:
                resolved.append(_resolve(guest))

        if problems:
            raise ValidationFailed(
                "The backup was not started because some targets are unavailable.",
                extra={"problems": problems},
            )
        return resolved

    async def _skip_reason(self, key: tuple[int, str]) -> str:
        vmid, guest_type = key
        guest = await self._guests.get_by_vmid(vmid, GuestType(guest_type))
        if guest is None:
            return f"{guest_type} {vmid}: no longer in the inventory"
        return f"{guest_type} {vmid} ({guest.name}): not enabled for backup"

    # ---- run creation --------------------------------------------------------

    async def request_manual_run(
        self, request: ManualBackupRequest, *, requested_by: int | None
    ) -> BackupRun:
        targets = await self.resolve_explicit_targets(request.targets)
        options = await self._manual_options(request)
        return await self._create_run(
            job_id=None,
            trigger=TriggerType.MANUAL,
            requested_by=requested_by,
            targets=targets,
            options=options,
        )

    async def request_job_run(
        self, job: BackupJob, *, trigger: TriggerType, requested_by: int | None
    ) -> BackupRun:
        existing = await self._runs.active_for_job(job.id)
        if existing is not None:
            raise Conflict(
                f"Job '{job.name}' already has run #{existing.id} in progress. "
                "Wait for it to finish, or cancel it first."
            )

        resolution = await self.resolve_job_targets(job)
        if not resolution.targets:
            detail = "; ".join(resolution.skipped) or "no guests are enabled for backup"
            raise ValidationFailed(
                f"Job '{job.name}' resolves to no guests: {detail}",
                extra={"skipped": resolution.skipped},
            )

        return await self._create_run(
            job_id=job.id,
            trigger=trigger,
            requested_by=requested_by,
            targets=resolution.targets,
            options=RunOptions(
                mode=BackupMode(job.mode),
                compression=Compression(job.compression),
                zstd_threads=job.zstd_threads,
                storage=job.storage,
                bwlimit_kbps=job.bwlimit_kbps,
                upload=job.upload_enabled,
                retention_source="job",
                keep_local=job.keep_local,
                keep_remote=job.keep_remote,
                notes=f"proxsync job '{job.name}'",
            ),
        )

    async def _create_run(
        self,
        *,
        job_id: int | None,
        trigger: TriggerType,
        requested_by: int | None,
        targets: list[ResolvedTarget],
        options: RunOptions,
    ) -> BackupRun:
        run = await self._runs.create(
            job_id=job_id,
            trigger=trigger,
            requested_by=requested_by,
            targets=[target.model_dump(mode="json") for target in targets],
            correlation_id=get_correlation_id() or "",
        )
        run.options = options.model_dump(mode="json")
        await self._runs.flush()

        logger.info(
            "backup_run_requested",
            run_id=run.id,
            job_id=job_id,
            trigger=trigger.value,
            guests=len(targets),
            storage=options.storage,
        )
        return run

    async def _manual_options(self, request: ManualBackupRequest) -> RunOptions:
        """Request overrides win; anything omitted falls back to the configured defaults."""
        defaults = await self._settings_service.get_section(SettingsSection.AGENT)
        assert isinstance(defaults, AgentSettings)
        gdrive = await self._settings_service.get_section(SettingsSection.GDRIVE)
        assert isinstance(gdrive, GDriveSettings)
        retention = await self._settings_service.get_section(SettingsSection.RETENTION)
        assert isinstance(retention, RetentionSettings)

        return RunOptions(
            mode=request.mode or defaults.default_mode,
            compression=request.compression or defaults.default_compression,
            zstd_threads=(
                request.zstd_threads
                if request.zstd_threads is not None
                else defaults.default_zstd_threads
            ),
            storage=request.storage or defaults.default_storage,
            bwlimit_kbps=(
                request.bwlimit_kbps
                if request.bwlimit_kbps is not None
                else defaults.default_bwlimit_kbps
            ),
            upload=gdrive.enabled if request.upload is None else request.upload,
            retention_source="global",
            keep_local=retention.keep_local,
            keep_remote=retention.keep_remote,
            notes=request.notes,
        )

    # ---- reads ---------------------------------------------------------------

    async def get_run(self, run_id: int) -> BackupRun:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFound(f"No backup run with id {run_id}")
        return run

    async def get_backup(self, backup_id: int) -> BackupHistory:
        record = await self._history.get(backup_id)
        if record is None:
            raise NotFound(f"No backup with id {backup_id}")
        return record

    async def set_retention_lock(self, backup_id: int, *, locked: bool) -> BackupHistory:
        record = await self.get_backup(backup_id)
        record.retention_locked = locked
        await self._history.flush()
        logger.info("backup_retention_lock_changed", backup_id=backup_id, locked=locked)
        return record

    async def delete_local_artifact(self, backup_id: int) -> tuple[str, AgentDeletedArtifact]:
        """Remove the artifact from the host and mark the row deleted.

        The row is never removed. `status: deleted` is the audit record of what used to be
        here — a backup that vanished from history without trace is indistinguishable from
        one that was never taken.
        """
        record = await self.get_backup(backup_id)

        if record.status == BackupStatus.RUNNING.value:
            raise Conflict(
                f"Backup #{backup_id} is still running. Cancel its run before deleting it."
            )
        if record.retention_locked:
            raise Conflict(
                f"Backup #{backup_id} is pinned against deletion. Unpin it first if you are "
                "sure you want it gone."
            )
        if self._restores is not None:
            # Retention already refuses to delete an artifact a restore names; a manual delete
            # has to refuse for the same reason, or the restore loses its source mid-flight.
            active = await self._restores.active_for_backup(backup_id)
            if active is not None:
                raise Conflict(
                    f"Restore #{active.id} is '{active.status}' and needs this artifact. "
                    "Cancel or finish it first."
                )
        if record.filename is None:
            raise ValidationFailed(
                f"Backup #{backup_id} never produced an artifact, so there is nothing to "
                "delete on the host."
            )
        if self._agent is None:  # pragma: no cover - wired by the composition root
            raise ConfigurationError("The agent client is not available in this context")

        filename = record.filename
        deleted = await self._agent.delete_backup(filename)

        record.local_deleted_at = datetime.now(UTC)
        # `status=deleted` means no usable copy remains anywhere. Keeping an uploaded remote
        # copy visible is essential: restore and remote retention must still be able to find
        # it after an operator removes only the local artifact.
        remote_exists = (
            record.upload_status == UploadStatus.UPLOADED.value
            and record.remote_path is not None
            and record.remote_deleted_at is None
        )
        record.status = BackupStatus.SUCCESS.value if remote_exists else BackupStatus.DELETED.value
        await self._history.flush()

        logger.info(
            "backup_deleted",
            backup_id=backup_id,
            filename=filename,
            freed_bytes=deleted.freed_bytes,
        )
        return filename, deleted

    async def cancel_queued_run(self, run_id: int) -> BackupRun:
        """Cancel a run that has not started. Cancelling one that *has* started requires the
        run executor, which owns the agent task id — see `BackupRunner.cancel`."""
        run = await self.get_run(run_id)
        if run.status != RunStatus.QUEUED.value:
            raise Conflict(f"Run #{run_id} is '{run.status}' and cannot be cancelled while queued.")
        run.status = RunStatus.CANCELLED.value
        run.finished_at = datetime.now(UTC)
        run.error_message = "Cancelled before it started"
        await self._runs.flush()
        return run


def _resolve(guest: Guest) -> ResolvedTarget:
    return ResolvedTarget(
        guest_id=guest.id,
        vmid=guest.vmid,
        guest_type=GuestType(guest.guest_type),
        guest_name=guest.name,
        node=guest.node,
    )


def summarise_run(run: BackupRun, records: list[BackupHistory]) -> RunStatus:
    """The run's terminal status, derived from its artifacts.

    `partial` exists so that a weekly job which backed up nine guests and lost one is not
    reported as a clean success, and not as a total failure either.
    """
    if not records:
        return RunStatus.FAILED

    succeeded = sum(1 for record in records if record.status == BackupStatus.SUCCESS.value)
    cancelled = sum(1 for record in records if record.status == BackupStatus.CANCELLED.value)

    if cancelled and succeeded == 0:
        return RunStatus.CANCELLED
    if succeeded == run.guest_total:
        return RunStatus.SUCCESS
    if succeeded == 0:
        return RunStatus.CANCELLED if cancelled else RunStatus.FAILED
    return RunStatus.PARTIAL


def run_payload(run: BackupRun) -> dict[str, Any]:
    """The subset of a run published on the event bus."""
    return {
        "run_id": run.id,
        "job_id": run.job_id,
        "status": run.status,
        "guest_total": run.guest_total,
        "guest_succeeded": run.guest_succeeded,
        "guest_failed": run.guest_failed,
    }
