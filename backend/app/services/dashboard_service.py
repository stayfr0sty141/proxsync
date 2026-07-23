"""Aggregation service backing the dashboard overview endpoints.

The dashboard is a read-only projection: it owns no tables and mutates nothing.
It composes rows other modules already maintain — guests, backup runs, jobs,
storage samples, restores and sync tasks — plus the agent's health, into the two
payloads the overview page renders. All aggregation is done here in Python over
existing repository methods rather than by adding bespoke SQL to each repository,
because the dashboard is low-frequency and the datasets are small; this keeps the
summary logic in one auditable place and avoids widening five repository
Protocols for one screen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.repositories.backup_history_repository import BackupHistoryRepository
from app.repositories.backup_job_repository import BackupJobRepository
from app.repositories.backup_run_repository import BackupRunRepository
from app.repositories.guest_repository import GuestRepository
from app.repositories.restore_repository import RestoreRepository
from app.repositories.storage_snapshot_repository import StorageSnapshotRepository
from app.repositories.sync_task_repository import SyncTaskRepository
from app.schemas.dashboard import (
    ActivityItemResponse,
    AgentSummaryResponse,
    DashboardActivityResponse,
    DashboardSummaryResponse,
    DriveStorageSummary,
    GuestCountsResponse,
    JobsSummaryResponse,
    LastBackupResponse,
    LocalStorageSummary,
    NextBackupResponse,
    StorageSummaryResponse,
)
from app.schemas.enums import GuestStatus, GuestType, RunStatus
from app.services.health_service import HealthService

# The activity feed merges three sources; this bounds how many of each we pull
# before the merge so a single busy source cannot crowd the others out.
_ACTIVITY_SOURCE_LIMIT = 25
_FINISHED_RUN_STATES = (
    RunStatus.SUCCESS.value,
    RunStatus.PARTIAL.value,
    RunStatus.FAILED.value,
)


class DashboardService:
    """Composes the dashboard summary and activity feed from existing rows."""

    def __init__(
        self,
        *,
        guests: GuestRepository,
        runs: BackupRunRepository,
        history: BackupHistoryRepository,
        jobs: BackupJobRepository,
        snapshots: StorageSnapshotRepository,
        restores: RestoreRepository,
        sync_tasks: SyncTaskRepository,
        health_service: HealthService,
    ) -> None:
        self._guests = guests
        self._runs = runs
        self._history = history
        self._jobs = jobs
        self._snapshots = snapshots
        self._restores = restores
        self._sync_tasks = sync_tasks
        self._health = health_service

    async def summary(self, *, now: datetime | None = None) -> DashboardSummaryResponse:
        """Build the overview payload for the dashboard's top region."""
        moment = now or datetime.now(UTC)

        return DashboardSummaryResponse(
            guests=await self._guest_counts(),
            last_backup=await self._last_backup(),
            next_backup=await self._next_backup(),
            storage=await self._storage_summary(),
            jobs=await self._jobs_summary(moment),
            agent=await self._agent_summary(),
        )

    async def _guest_counts(self) -> GuestCountsResponse:
        guests = await self._guests.list_all()
        vm_total = vm_running = lxc_total = lxc_running = 0
        for guest in guests:
            is_running = guest.status == GuestStatus.RUNNING.value
            if guest.guest_type == GuestType.VM.value:
                vm_total += 1
                vm_running += 1 if is_running else 0
            elif guest.guest_type == GuestType.LXC.value:
                lxc_total += 1
                lxc_running += 1 if is_running else 0
        return GuestCountsResponse(
            vm_total=vm_total,
            vm_running=vm_running,
            lxc_total=lxc_total,
            lxc_running=lxc_running,
        )

    async def _last_backup(self) -> LastBackupResponse | None:
        # list_recent is newest-first; the first row that has actually finished is
        # the most recent completed run. An in-flight run is deliberately skipped
        # so "last backup" reflects a finished outcome, not a pending one.
        recent = await self._runs.list_recent(limit=_ACTIVITY_SOURCE_LIMIT)
        finished = next(
            (run for run in recent if run.status in _FINISHED_RUN_STATES),
            None,
        )
        if finished is None:
            return None

        backups = await self._history.for_run(finished.id)
        size_bytes = sum(b.size_bytes for b in backups if b.size_bytes is not None)
        return LastBackupResponse(
            id=finished.id,
            finished_at=finished.finished_at,
            status=finished.status,
            guest_count=finished.guest_total,
            duration_seconds=finished.duration_seconds,
            size_bytes=size_bytes if backups else None,
        )

    async def _next_backup(self) -> NextBackupResponse | None:
        enabled = await self._jobs.list_enabled()
        scheduled = [job for job in enabled if job.next_run_at is not None]
        if not scheduled:
            return None
        # Soonest next_run_at wins. list_enabled orders by id, so we sort here
        # rather than relying on the repository's order.
        nxt = min(scheduled, key=lambda job: job.next_run_at)  # type: ignore[arg-type,return-value]
        return NextBackupResponse(
            job_id=nxt.id,
            name=nxt.name,
            next_run_at=nxt.next_run_at,
            cron=nxt.cron_expression,
            timezone=nxt.timezone,
        )

    async def _storage_summary(self) -> StorageSummaryResponse:
        # Read the most recent committed storage sample rather than probing the
        # host: the dashboard must render instantly and must never block on the
        # agent. A missing sample (fresh install, no sampler run yet) yields
        # zeros for local and nulls for Drive, so an absent metric reads as
        # "unknown" downstream rather than a misleading 0%.
        snapshot = await self._snapshots.latest()
        if snapshot is None:
            return StorageSummaryResponse(
                local=LocalStorageSummary(
                    total_bytes=0,
                    used_bytes=0,
                    free_bytes=0,
                    used_percent=0.0,
                ),
                gdrive=DriveStorageSummary(
                    used_bytes=None,
                    quota_bytes=None,
                    used_percent=None,
                ),
            )

        used_percent = (
            (snapshot.local_used_bytes / snapshot.local_total_bytes) * 100
            if snapshot.local_total_bytes > 0
            else 0.0
        )
        drive_percent = (
            (snapshot.remote_used_bytes / snapshot.remote_quota_bytes) * 100
            if snapshot.remote_used_bytes is not None and snapshot.remote_quota_bytes
            else None
        )
        return StorageSummaryResponse(
            local=LocalStorageSummary(
                total_bytes=snapshot.local_total_bytes,
                used_bytes=snapshot.local_used_bytes,
                free_bytes=snapshot.local_free_bytes,
                used_percent=round(used_percent, 2),
            ),
            gdrive=DriveStorageSummary(
                used_bytes=snapshot.remote_used_bytes,
                quota_bytes=snapshot.remote_quota_bytes,
                used_percent=round(drive_percent, 2) if drive_percent is not None else None,
            ),
        )

    async def _jobs_summary(self, now: datetime) -> JobsSummaryResponse:
        active = await self._runs.active()
        running = sum(1 for run in active if run.status == RunStatus.RUNNING.value)
        queued = sum(1 for run in active if run.status == RunStatus.QUEUED.value)

        failed = await self._runs.list_recent(limit=200, status=RunStatus.FAILED)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)
        failed_24h = sum(1 for run in failed if _run_moment(run) >= cutoff_24h)
        failed_7d = sum(1 for run in failed if _run_moment(run) >= cutoff_7d)
        return JobsSummaryResponse(
            running=running,
            queued=queued,
            failed_24h=failed_24h,
            failed_7d=failed_7d,
        )

    async def _agent_summary(self) -> AgentSummaryResponse:
        # The dashboard must render instantly and must never fail because the
        # agent is unreachable or returned something unexpected. Any error probing
        # health is reported as "not reachable" rather than propagated, so a sick
        # agent degrades one tile instead of taking down the whole overview.
        try:
            detail = await self._health.detail()
        except Exception:
            return AgentSummaryResponse(reachable=False, version=None, latency_ms=None)
        agent = next((c for c in detail.components if c.name == "agent"), None)
        if agent is None:
            return AgentSummaryResponse(reachable=False, version=None, latency_ms=None)
        version = agent.metadata.get("version")
        return AgentSummaryResponse(
            reachable=agent.status == "ok",
            version=str(version) if version is not None else None,
            latency_ms=agent.latency_ms,
        )

    async def activity(self, *, limit: int = 20) -> DashboardActivityResponse:
        """Merge recent backups, restores and transfers into one time-ordered feed."""
        runs = await self._runs.list_recent(limit=_ACTIVITY_SOURCE_LIMIT)
        restores = await self._restores.search(limit=_ACTIVITY_SOURCE_LIMIT)
        syncs = await self._sync_tasks.list_recent(limit=_ACTIVITY_SOURCE_LIMIT)

        items: list[ActivityItemResponse] = []
        for run in runs:
            label = (
                f"Scheduled backup run #{run.id}"
                if run.job_id is not None
                else f"Manual backup run #{run.id}"
            )
            items.append(
                ActivityItemResponse(
                    kind="backup",
                    id=run.id,
                    title=label,
                    status=run.status,
                    ts=_run_moment(run),
                    correlation_id=run.correlation_id,
                )
            )
        for restore in restores:
            items.append(
                ActivityItemResponse(
                    kind="restore",
                    id=restore.id,
                    title=f"Restore to VMID {restore.target_vmid}",
                    status=restore.status,
                    ts=_restore_moment(restore),
                    correlation_id=restore.correlation_id,
                )
            )
        for task in syncs:
            items.append(
                ActivityItemResponse(
                    kind="sync",
                    id=task.id,
                    title=f"{task.direction.capitalize()} transfer #{task.id}",
                    status=task.status,
                    ts=_sync_moment(task),
                    correlation_id=task.correlation_id,
                )
            )

        items.sort(key=lambda item: item.ts, reverse=True)
        return DashboardActivityResponse(items=items[: max(0, limit)])


def _run_moment(run: object) -> datetime:
    """Best available timestamp for ordering a run: finished, else started, else created."""
    finished = getattr(run, "finished_at", None)
    started = getattr(run, "started_at", None)
    created = getattr(run, "created_at", None)
    return finished or started or created or datetime.now(UTC)


def _restore_moment(restore: object) -> datetime:
    finished = getattr(restore, "finished_at", None)
    started = getattr(restore, "started_at", None)
    created = getattr(restore, "created_at", None)
    return finished or started or created or datetime.now(UTC)


def _sync_moment(task: object) -> datetime:
    finished = getattr(task, "finished_at", None)
    started = getattr(task, "started_at", None)
    created = getattr(task, "created_at", None)
    return finished or started or created or datetime.now(UTC)
