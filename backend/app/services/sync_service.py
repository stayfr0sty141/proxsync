"""Google Drive replication: what to upload, and what the two copies look like.

The same split as backups. This service decides and records; `app.workers.sync_worker` moves
the bytes. Intent is durable in `sync_tasks` before a transfer starts, so a crash mid-upload
resumes rather than being forgotten.

**Retention depends on this being honest.** M5 will delete local artifacts only once they are
uploaded, so a transfer reported as successful when it is not would eventually delete the only
copy of a backup. That is why a failed verification fails the upload, and why "the sizes match
but the remote publishes no hash" is reported as unverified rather than quietly accepted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.clients.agent_client import AgentClient
from app.core.errors import AgentError, AgentUnavailable, Conflict, NotFound, ValidationFailed
from app.core.logging import get_correlation_id, logger
from app.db.models.backup import BackupHistory, SyncTask
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.sync_task_repository import SqlAlchemySyncTaskRepository
from app.schemas.agent import AgentVerifyResult
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    GuestType,
    SettingsSection,
    SyncDirection,
    SyncStatus,
    UploadStatus,
)
from app.schemas.settings import GDriveSettings
from app.schemas.sync import (
    BrowserEntry,
    ComparisonResponse,
    ComparisonState,
    SyncQueueStatus,
    SyncTaskResponse,
    VerifyResponse,
)
from app.services.settings_service import SettingsService

MAX_BROWSER_ENTRIES = 2000


class SyncService:
    def __init__(
        self,
        *,
        tasks: SqlAlchemySyncTaskRepository,
        history: SqlAlchemyBackupHistoryRepository,
        settings_service: SettingsService,
        agent: AgentClient,
    ) -> None:
        self._tasks = tasks
        self._history = history
        self._settings_service = settings_service
        self._agent = agent

    # ---- configuration -------------------------------------------------------

    async def config(self) -> GDriveSettings:
        section = await self._settings_service.get_section(SettingsSection.GDRIVE)
        assert isinstance(section, GDriveSettings)
        return section

    async def _require_enabled(self) -> GDriveSettings:
        config = await self.config()
        if not config.enabled:
            raise ValidationFailed(
                "Google Drive sync is disabled. Enable it under Settings → Google Drive, "
                "and make sure the rclone remote is configured on the Proxmox host."
            )
        return config

    # ---- queueing ------------------------------------------------------------

    async def enqueue_upload(self, backup_id: int, *, force: bool = False) -> SyncTask:
        config = await self._require_enabled()
        record = await self._get_backup(backup_id)

        if record.status != BackupStatus.SUCCESS.value:
            raise ValidationFailed(
                f"Backup #{backup_id} is '{record.status}', so there is no artifact to upload."
            )
        if record.filename is None:
            raise ValidationFailed(f"Backup #{backup_id} produced no artifact.")
        if record.local_deleted_at is not None:
            raise ValidationFailed(
                f"Backup #{backup_id} has been deleted locally; there is nothing left to send."
            )

        existing = await self._tasks.active_for_backup(backup_id)
        if existing is not None:
            raise Conflict(
                f"Backup #{backup_id} is already queued for transfer as sync task #{existing.id}."
            )

        if record.upload_status == UploadStatus.UPLOADED.value and not force:
            raise Conflict(
                f"Backup #{backup_id} is already uploaded. Pass force=true to send it again "
                "— for instance if the remote copy is known to be bad."
            )

        task = await self._tasks.create(
            backup_id=backup_id,
            direction=SyncDirection.UPLOAD,
            remote_name=config.remote_name,
            remote_path=config.folder,
            max_attempts=config.upload_retry + 1,
            correlation_id=get_correlation_id(),
        )
        record.upload_status = UploadStatus.PENDING.value
        await self._tasks.flush()

        logger.info(
            "sync_upload_queued",
            sync_task_id=task.id,
            backup_id=backup_id,
            filename=record.filename,
            remote=config.remote_name,
        )
        return task

    async def enqueue_upload_by_filename(self, filename: str, *, force: bool = True) -> SyncTask:
        records = await self._history.search(query=filename, limit=10)
        matched = next((r for r in records if r.filename == filename), None)
        if matched is None:
            arts = await self._agent.list_backups()
            art = next((a for a in arts if a.filename == filename), None)
            if art is None:
                raise NotFound(f"Backup file '{filename}' not found on host agent")
            matched = await self._history.create(
                run_id=None,
                guest_id=None,
                vmid=art.vmid,
                guest_type=art.guest_type,
                guest_name=f"{art.guest_type.value.upper()} {art.vmid}",
                node="server",
                storage="hdd_3tb",
                mode=BackupMode.SNAPSHOT,
                compression=art.compression,
                upload_status=UploadStatus.NOT_REQUIRED,
                correlation_id=None,
                started_at=art.created_at,
            )
            matched.filename = art.filename
            matched.local_path = art.path
            matched.size_bytes = art.size_bytes
            matched.status = BackupStatus.SUCCESS.value
            matched.finished_at = art.created_at
            matched.checksum_sha256 = art.checksum_sha256
            await self._history.flush()

        return await self.enqueue_upload(matched.id, force=force)

    async def enqueue_pending(self) -> int:
        """Queue every successful backup still awaiting upload. Returns how many were added.

        The worker calls this each cycle rather than the backup runner calling it on success:
        a backup that completed while the sync worker was stopped still gets uploaded, and the
        two workers stay independent of each other.
        """
        config = await self.config()
        if not config.enabled:
            return 0

        queued = 0
        for record in await self._history.awaiting_upload():
            if await self._tasks.active_for_backup(record.id) is not None:
                continue
            await self._tasks.create(
                backup_id=record.id,
                direction=SyncDirection.UPLOAD,
                remote_name=config.remote_name,
                remote_path=config.folder,
                max_attempts=config.upload_retry + 1,
                correlation_id=record.correlation_id,
            )
            queued += 1

        if queued:
            logger.info("sync_pending_queued", count=queued)
        return queued

    # ---- verification --------------------------------------------------------

    async def verify_backup(self, backup_id: int) -> VerifyResponse:
        config = await self._require_enabled()
        record = await self._get_backup(backup_id)
        if record.filename is None:
            raise ValidationFailed(f"Backup #{backup_id} produced no artifact to verify.")

        result = await self._agent.verify_remote(
            filename=record.filename,
            remote=config.remote_name,
            remote_path=config.folder,
        )
        self._apply_verification(record, result)
        await self._history.flush()

        logger.info(
            "sync_verified",
            backup_id=backup_id,
            filename=record.filename,
            outcome=result.outcome.value,
        )
        return VerifyResponse(
            backup_id=backup_id,
            filename=record.filename,
            outcome=result.outcome,
            verified=result.verified,
            local_size_bytes=result.local_size_bytes,
            remote_size_bytes=result.remote_size_bytes,
            local_md5=result.local_md5,
            remote_md5=result.remote_md5,
            detail=result.detail,
            checked_at=datetime.now(UTC),
        )

    @staticmethod
    def _apply_verification(record: BackupHistory, result: AgentVerifyResult) -> None:
        """Record what verification found — including when it found the copy missing.

        A row that still claimed `uploaded` after the remote copy was found to be gone would
        let retention delete the local artifact on the strength of a file that is not there.
        """
        if result.verified:
            record.upload_status = UploadStatus.UPLOADED.value
            record.remote_size_bytes = result.remote_size_bytes
            record.remote_checksum = result.remote_md5
            if record.uploaded_at is None:
                record.uploaded_at = datetime.now(UTC)
            return

        record.remote_size_bytes = result.remote_size_bytes
        if record.upload_status == UploadStatus.UPLOADED.value:
            record.upload_status = UploadStatus.FAILED.value

    # ---- reads ---------------------------------------------------------------

    async def get_task(self, task_id: int) -> SyncTask:
        task = await self._tasks.get(task_id)
        if task is None:
            raise NotFound(f"No sync task with id {task_id}")
        return task

    async def queue_status(self, *, current: SyncTask | None = None) -> SyncQueueStatus:
        config = await self.config()
        return SyncQueueStatus(
            enabled=config.enabled,
            remote_name=config.remote_name,
            remote_path=config.folder,
            queued=await self._tasks.count(status=SyncStatus.QUEUED),
            running=await self._tasks.count(status=SyncStatus.RUNNING),
            failed=await self._tasks.count(status=SyncStatus.FAILED),
            pending_uploads=await self._history.count_awaiting_upload(),
            current=await self.to_response(current) if current is not None else None,
        )

    async def to_response(self, task: SyncTask) -> SyncTaskResponse:
        filename: str | None = None
        if task.backup_id is not None:
            record = await self._history.get(task.backup_id)
            filename = record.filename if record is not None else None

        percent: float | None = None
        if task.bytes_total:
            percent = round(task.bytes_transferred / task.bytes_total * 100, 1)

        return SyncTaskResponse(
            id=task.id,
            backup_id=task.backup_id,
            filename=filename,
            direction=SyncDirection(task.direction),
            status=SyncStatus(task.status),
            agent_task_id=task.agent_task_id,
            remote_name=task.remote_name,
            remote_path=task.remote_path,
            bytes_total=task.bytes_total,
            bytes_transferred=task.bytes_transferred,
            transfer_rate_bps=task.transfer_rate_bps,
            percent=percent,
            attempt=task.attempt,
            max_attempts=task.max_attempts,
            next_retry_at=task.next_retry_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            error_message=task.error_message,
            created_at=task.created_at,
        )

    # ---- browser -------------------------------------------------------------

    async def local_entries(self) -> list[BrowserEntry]:
        """What is actually on the HDD, from the agent — not from ProxSync's history.

        The two can differ: an artifact created by a `vzdump` run outside ProxSync is real and
        occupies real space, and a browser that hid it would misrepresent the disk.
        """
        artifacts = await self._agent.list_backups()
        known = {
            record.filename: record.id
            for record in await self._history.search(limit=MAX_BROWSER_ENTRIES)
            if record.filename
        }
        return [
            BrowserEntry(
                filename=item.filename,
                vmid=item.vmid,
                guest_type=item.guest_type,
                size_bytes=item.size_bytes,
                created_at=item.created_at,
                checksum_sha256=item.checksum_sha256,
                backup_id=known.get(item.filename),
            )
            for item in artifacts
        ]

    async def remote_entries(self) -> list[BrowserEntry]:
        config = await self._require_enabled()
        listing = await self._agent.list_remote(
            remote=config.remote_name, remote_path=config.folder
        )
        known = {
            record.filename: record
            for record in await self._history.search(limit=MAX_BROWSER_ENTRIES)
            if record.filename
        }

        entries: list[BrowserEntry] = []
        for item in listing.entries:
            if item.is_dir:
                continue
            record = known.get(item.name)
            entries.append(
                BrowserEntry(
                    filename=item.name,
                    vmid=record.vmid if record else None,
                    guest_type=GuestType(record.guest_type) if record else None,
                    size_bytes=item.size_bytes,
                    created_at=item.modified_at,
                    md5=item.md5,
                    backup_id=record.id if record else None,
                )
            )
        return entries

    async def compare(self) -> ComparisonResponse:
        """Local versus remote, by filename and size."""
        config = await self.config()

        local = {entry.filename: entry for entry in await self.local_entries()}

        if not config.enabled:
            disabled_states = [
                self._compare_one(filename, entry, None)
                for filename, entry in sorted(local.items())
            ]
            return ComparisonResponse(
                remote=config.remote_name or "gdrive",
                remote_path=config.folder or "dump",
                in_sync=0,
                local_only=len(disabled_states),
                remote_only=0,
                size_mismatch=0,
                entries=disabled_states,
                detail=(
                    "Google Drive sync is disabled. Enable it under Settings → Google Drive "
                    "to enable cloud sync."
                ),
            )

        try:
            remote = {entry.filename: entry for entry in await self.remote_entries()}
        except (AgentUnavailable, AgentError) as exc:
            # A partial answer must never read as "everything is in sync".
            return ComparisonResponse(
                remote=config.remote_name,
                remote_path=config.folder,
                in_sync=0,
                local_only=0,
                remote_only=0,
                size_mismatch=0,
                entries=[],
                detail=f"The remote could not be listed: {exc.detail}",
            )

        states: list[ComparisonState] = []
        for filename in sorted(set(local) | set(remote)):
            states.append(self._compare_one(filename, local.get(filename), remote.get(filename)))

        counts = dict.fromkeys(("in_sync", "local_only", "remote_only", "size_mismatch"), 0)
        for state in states:
            counts[state.state] += 1

        return ComparisonResponse(
            remote=config.remote_name,
            remote_path=config.folder,
            in_sync=counts["in_sync"],
            local_only=counts["local_only"],
            remote_only=counts["remote_only"],
            size_mismatch=counts["size_mismatch"],
            entries=states,
        )

    @staticmethod
    def _compare_one(
        filename: str, local: BrowserEntry | None, remote: BrowserEntry | None
    ) -> ComparisonState:
        backup_id = (local.backup_id if local else None) or (remote.backup_id if remote else None)
        base: dict[str, Any] = {"filename": filename, "backup_id": backup_id}

        if local is None:
            return ComparisonState(
                **base, state="remote_only", remote_size_bytes=remote.size_bytes if remote else None
            )
        if remote is None:
            return ComparisonState(**base, state="local_only", local_size_bytes=local.size_bytes)

        state = "in_sync" if local.size_bytes == remote.size_bytes else "size_mismatch"
        return ComparisonState(
            **base,
            state=state,
            local_size_bytes=local.size_bytes,
            remote_size_bytes=remote.size_bytes,
        )

    async def _get_backup(self, backup_id: int) -> BackupHistory:
        record = await self._history.get(backup_id)
        if record is None:
            raise NotFound(f"No backup with id {backup_id}")
        return record
