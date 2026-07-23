"""Response models for the dashboard aggregate endpoints.

These mirror the ``DashboardSummary`` and ``DashboardActivityResponse`` shapes the
frontend consumes (docs/API.md § Dashboard). The dashboard is a read-only
projection over data other modules already own — guests, runs, jobs, storage
samples and the agent's health — so it introduces no new persisted state; it only
composes existing rows into the summary the overview page renders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GuestCountsResponse(BaseModel):
    """Fleet composition split by guest type and running state."""

    vm_total: int
    vm_running: int
    lxc_total: int
    lxc_running: int


class LastBackupResponse(BaseModel):
    """The most recently finished backup run, or ``None`` if none has run."""

    id: int
    finished_at: datetime | None
    status: str
    guest_count: int
    duration_seconds: float | None
    size_bytes: int | None


class NextBackupResponse(BaseModel):
    """The soonest-firing enabled schedule, or ``None`` if none is enabled."""

    job_id: int
    name: str
    next_run_at: datetime | None
    cron: str
    timezone: str


class LocalStorageSummary(BaseModel):
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_percent: float


class DriveStorageSummary(BaseModel):
    used_bytes: int | None
    quota_bytes: int | None
    used_percent: float | None


class StorageSummaryResponse(BaseModel):
    local: LocalStorageSummary
    gdrive: DriveStorageSummary


class JobsSummaryResponse(BaseModel):
    """Run-queue health: what is in flight now and what failed recently."""

    running: int
    queued: int
    failed_24h: int
    failed_7d: int


class AgentSummaryResponse(BaseModel):
    """Agent reachability, surfaced from the health check.

    Only reachability, version and latency are exposed — nothing resembling a
    history — because the dashboard's job is an at-a-glance status, not tracking.
    """

    reachable: bool
    version: str | None
    latency_ms: float | None


class DashboardSummaryResponse(BaseModel):
    """The complete overview payload backing the dashboard's top region."""

    guests: GuestCountsResponse
    last_backup: LastBackupResponse | None
    next_backup: NextBackupResponse | None
    storage: StorageSummaryResponse
    jobs: JobsSummaryResponse
    agent: AgentSummaryResponse


ActivityKind = Literal["backup", "restore", "sync"]


class ActivityItemResponse(BaseModel):
    """One entry in the merged recent-activity feed."""

    kind: ActivityKind
    id: int
    title: str
    status: str
    ts: datetime
    correlation_id: str | None = None


class DashboardActivityResponse(BaseModel):
    items: list[ActivityItemResponse] = Field(default_factory=list)
