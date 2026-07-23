"""ORM row → response model mappings.

Written out by hand rather than leaning on `from_attributes`, for two reasons: the columns
are `str` where the API is a `StrEnum`, so a typo becomes a build error instead of a silently
wrong string; and relationships (`BackupJob.targets`) have to become value objects.

They live here, outside any one route module, so that `/runs` and `/backups` can each render
the other's rows without importing each other.
"""

from __future__ import annotations

from typing import Any

from app.db.models.backup import BackupHistory, BackupJob, BackupRun, RestoreHistory
from app.db.models.guest import Guest
from app.schemas.backup import (
    BackupHistoryResponse,
    BackupJobResponse,
    BackupRunResponse,
    GuestTarget,
    RunProgress,
)
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestStatus,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    RunStatus,
    TargetSelector,
    TriggerType,
    UploadStatus,
)
from app.schemas.guests import GuestResponse
from app.schemas.restore import PreflightReport, RestoreResponse


def guest_to_response(guest: Guest) -> GuestResponse:
    return GuestResponse(
        id=guest.id,
        vmid=guest.vmid,
        guest_type=GuestType(guest.guest_type),
        name=guest.name,
        node=guest.node,
        status=GuestStatus(guest.status),
        backup_enabled=guest.backup_enabled,
        tags=list(guest.tags or []),
        first_seen_at=guest.first_seen_at,
        last_seen_at=guest.last_seen_at,
    )


def job_to_response(job: BackupJob) -> BackupJobResponse:
    return BackupJobResponse(
        id=job.id,
        name=job.name,
        enabled=job.enabled,
        cron_expression=job.cron_expression,
        timezone=job.timezone,
        mode=BackupMode(job.mode),
        compression=Compression(job.compression),
        zstd_threads=job.zstd_threads,
        storage=job.storage,
        target_selector=TargetSelector(job.target_selector),
        targets=[
            GuestTarget(vmid=target.vmid, guest_type=GuestType(target.guest_type))
            for target in job.targets
        ],
        keep_local=job.keep_local,
        keep_remote=job.keep_remote,
        upload_enabled=job.upload_enabled,
        notify_on_start=job.notify_on_start,
        notify_on_success=job.notify_on_success,
        notify_on_failure=job.notify_on_failure,
        bwlimit_kbps=job.bwlimit_kbps,
        max_parallel=job.max_parallel,
        last_run_at=job.last_run_at,
        next_run_at=job.next_run_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def run_to_response(
    run: BackupRun, *, job_name: str | None = None, progress: RunProgress | None = None
) -> BackupRunResponse:
    return BackupRunResponse(
        id=run.id,
        job_id=run.job_id,
        job_name=job_name,
        trigger=TriggerType(run.trigger),
        status=RunStatus(run.status),
        guest_total=run.guest_total,
        guest_succeeded=run.guest_succeeded,
        guest_failed=run.guest_failed,
        targets=list(run.targets or []),
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=run.duration_seconds,
        error_message=run.error_message,
        correlation_id=run.correlation_id,
        created_at=run.created_at,
        progress=progress,
    )


def backup_to_response(record: BackupHistory) -> BackupHistoryResponse:
    return BackupHistoryResponse(
        id=record.id,
        run_id=record.run_id,
        vmid=record.vmid,
        guest_type=GuestType(record.guest_type),
        guest_name=record.guest_name,
        node=record.node,
        storage=record.storage,
        filename=record.filename,
        local_path=record.local_path,
        size_bytes=record.size_bytes,
        mode=BackupMode(record.mode),
        compression=Compression(record.compression),
        checksum_sha256=record.checksum_sha256,
        status=BackupStatus(record.status),
        agent_task_id=record.agent_task_id,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_seconds=record.duration_seconds,
        error_message=record.error_message,
        warnings=list(record.warnings) if record.warnings else None,
        upload_status=UploadStatus(record.upload_status),
        uploaded_at=record.uploaded_at,
        remote_path=record.remote_path,
        retention_locked=record.retention_locked,
        local_deleted_at=record.local_deleted_at,
        remote_deleted_at=record.remote_deleted_at,
        correlation_id=record.correlation_id,
        created_at=record.created_at,
    )


def restore_to_response(
    record: RestoreHistory, *, source_backup: BackupHistory | None = None
) -> RestoreResponse:
    return RestoreResponse(
        id=record.id,
        backup_id=record.backup_id,
        source=RestoreSource(record.source),
        filename=source_backup.filename if source_backup else None,
        source_vmid=source_backup.vmid if source_backup else None,
        target_vmid=record.target_vmid,
        target_type=GuestType(record.target_type),
        restore_mode=RestoreMode(record.restore_mode),
        target_node=record.target_node,
        target_storage=record.target_storage,
        overwrite_existing=record.overwrite_existing,
        force_stop=record.force_stop,
        start_after_restore=record.start_after_restore,
        status=RestoreStatus(record.status),
        confirmation_expires_at=record.confirmation_expires_at,
        confirmed_at=record.confirmed_at,
        preflight=_preflight(record.preflight),
        agent_task_id=record.agent_task_id,
        requested_by=record.requested_by,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_seconds=record.duration_seconds,
        error_message=record.error_message,
        correlation_id=record.correlation_id,
        created_at=record.created_at,
    )


def _preflight(stored: dict[str, Any] | None) -> PreflightReport | None:
    """Read the stored report back, tolerating a shape written by an older version.

    A report is a *record* of what was checked, not live state. If the schema has moved on
    since it was written, the honest answer is to omit it rather than to fail the whole
    response or, worse, to render it as though it still means what it says.
    """
    if not stored:
        return None
    try:
        return PreflightReport.model_validate(stored)
    except ValueError:
        return None
