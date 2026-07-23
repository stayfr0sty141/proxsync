"""Tests for the dashboard aggregate endpoints (M8).

The dashboard is a read-only projection over guests, runs, jobs, storage samples,
restores and sync tasks. These tests seed those rows directly and assert the two
endpoints compose them into the shapes the frontend consumes (docs/API.md), plus
the edge cases that would otherwise mislead an operator: an empty install must
return nulls rather than fabricated zeros, an in-flight run must not be reported
as the "last backup", and the activity feed must merge and order the three
sources newest-first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.backup import BackupRun
from app.db.models.guest import Guest
from app.schemas.enums import GuestStatus, GuestType, RunStatus
from tests.conftest import ApiClient

pytestmark = pytest.mark.anyio


async def _seed(session_factory: async_sessionmaker[AsyncSession], *rows: object) -> None:
    async with session_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


def _guest(vmid: int, *, guest_type: GuestType, running: bool) -> Guest:
    now = datetime.now(UTC)
    return Guest(
        vmid=vmid,
        guest_type=guest_type.value,
        name=f"guest-{vmid}",
        node="pve",
        status=GuestStatus.RUNNING.value if running else GuestStatus.STOPPED.value,
        backup_enabled=True,
        first_seen_at=now,
        last_seen_at=now,
    )


def _run(*, status: RunStatus, finished_minutes_ago: int | None, guest_total: int = 1) -> BackupRun:
    now = datetime.now(UTC)
    finished = (
        None if finished_minutes_ago is None else now - timedelta(minutes=finished_minutes_ago)
    )
    return BackupRun(
        job_id=None,
        trigger="manual",
        status=status.value,
        requested_by=None,
        guest_total=guest_total,
        guest_succeeded=guest_total if status is RunStatus.SUCCESS else 0,
        guest_failed=0,
        started_at=now - timedelta(minutes=(finished_minutes_ago or 0) + 5),
        finished_at=finished,
        duration_seconds=300.0 if finished else None,
        correlation_id=f"corr-{status.value}-{finished_minutes_ago}",
        created_at=now - timedelta(minutes=(finished_minutes_ago or 0) + 5),
    )


class TestDashboardSummary:
    async def test_counts_guests_by_type_and_running_state(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed(
            session_factory,
            _guest(101, guest_type=GuestType.VM, running=True),
            _guest(102, guest_type=GuestType.VM, running=False),
            _guest(201, guest_type=GuestType.LXC, running=True),
        )

        body = authenticated_client.get("/api/v1/dashboard/summary").json()

        assert body["guests"] == {
            "vm_total": 2,
            "vm_running": 1,
            "lxc_total": 1,
            "lxc_running": 1,
        }

    async def test_empty_install_returns_nulls_not_zeros(
        self,
        authenticated_client: ApiClient,
    ) -> None:
        body = authenticated_client.get("/api/v1/dashboard/summary").json()

        assert body["last_backup"] is None
        assert body["next_backup"] is None
        # Storage with no sample yet is zeroed locally and null for Drive, so an
        # absent metric never reads as a real 0%.
        assert body["storage"]["gdrive"]["used_bytes"] is None
        assert body["storage"]["local"]["total_bytes"] == 0

    async def test_last_backup_skips_an_in_flight_run(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A finished success plus a currently-running run: the summary's
        # "last backup" must be the finished one, not the in-flight one.
        await _seed(
            session_factory,
            _run(status=RunStatus.SUCCESS, finished_minutes_ago=30),
            _run(status=RunStatus.RUNNING, finished_minutes_ago=None),
        )

        body = authenticated_client.get("/api/v1/dashboard/summary").json()

        assert body["last_backup"] is not None
        assert body["last_backup"]["status"] == "success"

    async def test_counts_recent_failures_in_the_window(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed(
            session_factory,
            _run(status=RunStatus.FAILED, finished_minutes_ago=60),
            _run(status=RunStatus.FAILED, finished_minutes_ago=60 * 24 * 3),
        )

        body = authenticated_client.get("/api/v1/dashboard/summary").json()

        # One failure in the last 24h, both within 7 days.
        assert body["jobs"]["failed_24h"] == 1
        assert body["jobs"]["failed_7d"] == 2

    async def test_requires_authentication(self, client: ApiClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 401


class TestDashboardActivity:
    async def test_merges_and_orders_newest_first(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed(
            session_factory,
            _run(status=RunStatus.SUCCESS, finished_minutes_ago=10),
            _run(status=RunStatus.FAILED, finished_minutes_ago=120),
        )

        body = authenticated_client.get("/api/v1/dashboard/activity?limit=10").json()

        assert len(body["items"]) == 2
        # Newest (10 minutes ago) first.
        timestamps = [item["ts"] for item in body["items"]]
        assert timestamps == sorted(timestamps, reverse=True)
        assert body["items"][0]["kind"] == "backup"

    async def test_limit_is_capped(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed(
            session_factory,
            *[_run(status=RunStatus.SUCCESS, finished_minutes_ago=i + 1) for i in range(5)],
        )

        body = authenticated_client.get("/api/v1/dashboard/activity?limit=3").json()
        assert len(body["items"]) == 3

    async def test_rejects_out_of_range_limit(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.get("/api/v1/dashboard/activity?limit=0")
        assert response.status_code == 422
