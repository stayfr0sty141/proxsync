"""Worker-health regressions for the M5 retention and storage lanes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from app.core.config import Settings
from app.services.health_service import HealthService


def _service(
    settings: Settings, *, retention_running: bool = True, sample_error: str | None = None
) -> HealthService:
    running = SimpleNamespace(running=True)
    runner = SimpleNamespace(running=True, current_run_id=None)
    scheduler = SimpleNamespace(running=True, job_count=lambda: 0)
    sync = SimpleNamespace(running=True, current_task_id=None)
    retention = SimpleNamespace(running=retention_running, last_error=None)
    sampler = SimpleNamespace(
        running=True,
        last_sample_at=datetime.now(UTC),
        last_error=sample_error,
    )
    return HealthService(
        settings=settings.model_copy(
            update={"background_workers": True, "scheduler_enabled": True}
        ),
        database=cast(Any, running),
        agent=cast(Any, running),
        started_at=datetime.now(UTC),
        runner=cast(Any, runner),
        scheduler=cast(Any, scheduler),
        sync_worker=cast(Any, sync),
        retention_worker=cast(Any, retention),
        sampler=cast(Any, sampler),
    )


def test_workers_health_includes_the_retention_lane(settings: Settings) -> None:
    result = _service(settings, retention_running=False)._check_workers()  # noqa: SLF001

    assert result.status == "degraded"
    assert result.detail is not None
    assert "retention worker is not running" in result.detail


def test_workers_health_surfaces_a_storage_sampling_error(settings: Settings) -> None:
    result = _service(settings, sample_error="agent offline")._check_workers()  # noqa: SLF001

    assert result.status == "degraded"
    assert result.detail is not None
    assert "storage sampler is degraded: agent offline" in result.detail
