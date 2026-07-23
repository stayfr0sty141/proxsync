"""Health reporting.

A health check reports; it never raises. A component that is not configured is distinct from
one that is broken — "Telegram not configured" is a setup step, "agent unreachable" is an
incident, and conflating them makes the dashboard cry wolf.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app import __version__
from app.clients.agent_client import AgentClient
from app.clients.proxmox_client import ProxmoxClient
from app.core.config import Settings
from app.core.errors import AgentError, AgentUnavailable, AppError, ConfigurationError
from app.db.session import Database
from app.schemas.health import ComponentHealth, HealthDetailResponse
from app.workers.backup_runner import BackupRunner
from app.workers.scheduler import SchedulerWorker

if TYPE_CHECKING:
    from app.workers.log_writer import LogWriter
    from app.workers.notification_worker import NotificationWorker
    from app.workers.restore_worker import RestoreWorker
    from app.workers.retention_worker import RetentionWorker
    from app.workers.storage_sampler import StorageSampler
    from app.workers.sync_worker import SyncWorker


class HealthService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        agent: AgentClient,
        started_at: datetime,
        proxmox: ProxmoxClient | None = None,
        runner: BackupRunner | None = None,
        scheduler: SchedulerWorker | None = None,
        sync_worker: SyncWorker | None = None,
        restore_worker: RestoreWorker | None = None,
        retention_worker: RetentionWorker | None = None,
        sampler: StorageSampler | None = None,
        notification_worker: NotificationWorker | None = None,
        log_writer: LogWriter | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._agent = agent
        self._started_at = started_at
        self._proxmox = proxmox
        self._runner = runner
        self._scheduler = scheduler
        self._sync_worker = sync_worker
        self._restore_worker = restore_worker
        self._retention_worker = retention_worker
        self._sampler = sampler
        self._notification_worker = notification_worker
        self._log_writer = log_writer

    async def detail(self) -> HealthDetailResponse:
        components = [
            await self._check_database(),
            await self._check_agent(),
            await self._check_proxmox(),
            self._check_workers(),
        ]

        if any(component.status == "unavailable" for component in components):
            overall = "unavailable"
        elif any(component.status == "degraded" for component in components):
            overall = "degraded"
        else:
            overall = "ok"

        now = datetime.now(UTC)
        return HealthDetailResponse(
            status=overall,
            version=__version__,
            environment=self._settings.environment,
            started_at=self._started_at,
            uptime_seconds=round((now - self._started_at).total_seconds(), 1),
            components=components,
        )

    async def _check_database(self) -> ComponentHealth:
        started = time.perf_counter()
        healthy = await self._database.check()
        latency = round((time.perf_counter() - started) * 1000, 2)
        return ComponentHealth(
            name="database",
            status="ok" if healthy else "unavailable",
            detail=None if healthy else "The database did not answer a trivial query",
            latency_ms=latency,
            metadata={"dialect": "sqlite" if self._settings.is_sqlite else "postgresql"},
        )

    async def _check_agent(self) -> ComponentHealth:
        if not self._agent.configured:
            return ComponentHealth(
                name="agent",
                status="not_configured",
                detail="PROXSYNC_AGENT_HMAC_SECRET is not set",
            )

        started = time.perf_counter()
        try:
            health = await self._agent.health()
        except AgentUnavailable as exc:
            return ComponentHealth(
                name="agent",
                status="unavailable",
                detail=str(exc.detail),
                metadata={"circuit": self._agent.circuit_state.value},
            )
        except AgentError as exc:
            return ComponentHealth(name="agent", status="degraded", detail=str(exc.detail))

        latency = round((time.perf_counter() - started) * 1000, 2)
        return ComponentHealth(
            name="agent",
            # The agent reports "degraded" itself when a Proxmox binary is missing or the
            # dump root is not writable; that is passed through rather than flattened to "ok".
            status="ok" if health.status == "ok" else "degraded",
            detail=None if health.status == "ok" else f"Agent reports '{health.status}'",
            latency_ms=latency,
            metadata={
                "version": health.version,
                "hostname": health.hostname,
                "mtls": health.mtls_enabled,
                "active_tasks": health.active_tasks,
                "dump_root_writable": health.dump_root_writable,
            },
        )

    async def _check_proxmox(self) -> ComponentHealth:
        if not self._settings.proxmox_configured or self._proxmox is None:
            return ComponentHealth(
                name="proxmox",
                status="not_configured",
                detail="PROXSYNC_PROXMOX_TOKEN_ID / _SECRET are not set",
            )

        started = time.perf_counter()
        try:
            version = await self._proxmox.version()
        except AppError as exc:
            # An expired token and an unreachable host are different problems with different
            # fixes, and the client already distinguishes them.
            return ComponentHealth(
                name="proxmox",
                status="degraded" if isinstance(exc, ConfigurationError) else "unavailable",
                detail=str(exc.detail),
                metadata={"node": self._settings.proxmox_node},
            )

        return ComponentHealth(
            name="proxmox",
            status="ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            metadata={
                "node": self._settings.proxmox_node,
                "version": version.version,
                "release": version.release,
            },
        )

    def _check_workers(self) -> ComponentHealth:
        if not self._settings.background_workers:
            return ComponentHealth(
                name="workers",
                status="not_configured",
                detail="Background workers are disabled (PROXSYNC_BACKGROUND_WORKERS=false)",
            )

        if self._runner is None or not self._runner.running:
            # Nothing would execute a queued backup, and nothing says so on any other page.
            return ComponentHealth(
                name="workers",
                status="unavailable",
                detail="The run executor is not running; queued backups will not start",
            )

        degraded = [*self._stopped_workers(), *self._sampler_problems()]
        if self._retention_worker is not None and self._retention_worker.last_error:
            degraded.append(
                f"The retention worker is degraded: {self._retention_worker.last_error}"
            )
        if self._notification_worker is not None and self._notification_worker.last_error:
            # A failing outbox is degraded, not unavailable: nothing is lost, but the operator
            # is not being told things — and that is exactly the state that hides other faults.
            degraded.append(
                f"Notification delivery is failing: {self._notification_worker.last_error}"
            )
        if self._log_writer is not None and self._log_writer.dropped:
            degraded.append(
                f"{self._log_writer.dropped} log entries were dropped before they could be "
                "persisted"
            )

        return ComponentHealth(
            name="workers",
            status="degraded" if degraded else "ok",
            detail="; ".join(degraded) or None,
            metadata=self._worker_metadata(),
        )

    def _stopped_workers(self) -> list[str]:
        """Every worker that should be running and is not, named individually.

        "Workers degraded" is not actionable; "the restore executor is not running" is. Only
        the run executor is fatal on its own — the others degrade a specific capability.
        """
        expected: list[tuple[bool, str]] = [
            (
                not self._settings.scheduler_enabled
                or (self._scheduler is not None and self._scheduler.running),
                "The scheduler is not running",
            ),
            (
                self._sync_worker is not None and self._sync_worker.running,
                "The upload worker is not running",
            ),
            (
                self._restore_worker is not None and self._restore_worker.running,
                # A confirmed restore would sit in the queue, and one left running by the
                # previous process would never be reconciled.
                "The restore executor is not running",
            ),
            (
                self._retention_worker is not None and self._retention_worker.running,
                "The retention worker is not running",
            ),
            (
                self._sampler is not None and self._sampler.running,
                "The storage sampler is not running",
            ),
            (
                self._notification_worker is not None and self._notification_worker.running,
                "The notification worker is not running; queued messages will not be delivered",
            ),
            (
                not self._settings.log_persist_enabled
                or (self._log_writer is not None and self._log_writer.running),
                "The log writer is not running; /logs will stop filling",
            ),
        ]
        return [reason for healthy, reason in expected if not healthy]

    def _sampler_problems(self) -> list[str]:
        if self._sampler is None or not self._sampler.running:
            return []
        problems: list[str] = []
        if self._sampler.last_error:
            problems.append(f"The storage sampler is degraded: {self._sampler.last_error}")
        if self._sampler.last_sample_at is None:
            problems.append("The storage sampler has not completed a sample")
        elif (
            datetime.now(UTC) - self._sampler.last_sample_at
        ).total_seconds() > self._settings.storage_sample_interval_seconds * 2:
            problems.append("The storage sampler's last successful sample is stale")
        return problems

    def _worker_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "current_run_id": self._runner.current_run_id if self._runner else None,
            "scheduled_jobs": self._scheduler.job_count() if self._scheduler else 0,
            "current_sync_task_id": (
                self._sync_worker.current_task_id if self._sync_worker else None
            ),
            "current_restore_id": (
                self._restore_worker.current_restore_id if self._restore_worker else None
            ),
            "retention_last_error": (
                self._retention_worker.last_error if self._retention_worker else None
            ),
        }
        if self._sampler is not None:
            metadata["storage_last_sample_at"] = self._sampler.last_sample_at
            metadata["storage_last_error"] = self._sampler.last_error
        if self._notification_worker is not None:
            metadata["notification_last_error"] = self._notification_worker.last_error
        if self._log_writer is not None:
            metadata["logs_written"] = self._log_writer.written
            metadata["logs_pending"] = self._log_writer.pending
            metadata["logs_dropped"] = self._log_writer.dropped
        return metadata
