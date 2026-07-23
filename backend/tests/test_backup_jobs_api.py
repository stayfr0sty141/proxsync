"""Backup schedule endpoints."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.enums import GuestType

from .conftest import ApiClient, make_guest, seed_guests

WEEKLY: dict[str, Any] = {
    "name": "Weekly Full",
    "cron_expression": "0 1 * * 0",
    "timezone": "Asia/Jakarta",
    "mode": "snapshot",
    "compression": "zstd",
    "storage": "backup-hdd",
    "target_selector": "all",
    "keep_local": 2,
    "keep_remote": 2,
}


class TestCreate:
    async def test_creates_the_schedule_from_the_brief(
        self, authenticated_client: ApiClient
    ) -> None:
        response = authenticated_client.post("/api/v1/backup-jobs", WEEKLY)

        assert response.status_code == 201
        body = response.json()
        assert body["cron_expression"] == "0 1 * * 0"
        assert body["timezone"] == "Asia/Jakarta"
        assert body["next_run_at"] is not None

    async def test_next_run_is_a_sunday_at_one_in_jakarta(
        self, authenticated_client: ApiClient
    ) -> None:
        """The specified schedule is weekly on Sunday; the stored preview must agree."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        body = authenticated_client.post("/api/v1/backup-jobs", WEEKLY).json()

        next_run = datetime.fromisoformat(body["next_run_at"]).astimezone(ZoneInfo("Asia/Jakarta"))
        assert next_run.strftime("%A") == "Sunday"
        assert (next_run.hour, next_run.minute) == (1, 0)

    async def test_rejects_a_malformed_cron(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.post(
            "/api/v1/backup-jobs", {**WEEKLY, "cron_expression": "0 1 * *"}
        )

        assert response.status_code == 400
        assert "cron expression" in response.json()["detail"]

    async def test_rejects_an_unknown_timezone(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.post(
            "/api/v1/backup-jobs", {**WEEKLY, "timezone": "Mars/Olympus_Mons"}
        )
        assert response.status_code == 400

    async def test_rejects_a_duplicate_name(self, authenticated_client: ApiClient) -> None:
        authenticated_client.post("/api/v1/backup-jobs", WEEKLY)
        response = authenticated_client.post("/api/v1/backup-jobs", WEEKLY)

        assert response.status_code == 409

    async def test_selector_all_refuses_an_explicit_target_list(
        self, authenticated_client: ApiClient
    ) -> None:
        """Otherwise 'all' plus a list reads as 'these', and would silently mean 'everything'."""
        response = authenticated_client.post(
            "/api/v1/backup-jobs",
            {**WEEKLY, "targets": [{"vmid": 101, "guest_type": "vm"}]},
        )

        assert response.status_code == 422

    async def test_selector_include_requires_targets(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.post(
            "/api/v1/backup-jobs", {**WEEKLY, "target_selector": "include"}
        )
        assert response.status_code == 422

    async def test_rejects_a_duplicated_target(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.post(
            "/api/v1/backup-jobs",
            {
                **WEEKLY,
                "target_selector": "include",
                "targets": [
                    {"vmid": 101, "guest_type": "vm"},
                    {"vmid": 101, "guest_type": "vm"},
                ],
            },
        )
        assert response.status_code == 422

    async def test_the_same_vmid_as_vm_and_container_is_refused(
        self, authenticated_client: ApiClient
    ) -> None:
        """Proxmox VMIDs are unique across VMs and containers, so this cannot be real. It has
        to be a 422 that says so, not a 500 from the unique constraint underneath."""
        response = authenticated_client.post(
            "/api/v1/backup-jobs",
            {
                **WEEKLY,
                "target_selector": "include",
                "targets": [
                    {"vmid": 101, "guest_type": "vm"},
                    {"vmid": 101, "guest_type": "lxc"},
                ],
            },
        )

        assert response.status_code == 422
        assert "unique across VMs and containers" in str(response.json()["errors"])


class TestUpdate:
    async def test_replaces_the_target_list(self, authenticated_client: ApiClient) -> None:
        created = authenticated_client.post(
            "/api/v1/backup-jobs",
            {
                **WEEKLY,
                "target_selector": "include",
                "targets": [
                    {"vmid": 101, "guest_type": "vm"},
                    {"vmid": 102, "guest_type": "vm"},
                ],
            },
        ).json()

        response = authenticated_client.put(
            f"/api/v1/backup-jobs/{created['id']}",
            {
                **WEEKLY,
                "target_selector": "include",
                "targets": [{"vmid": 201, "guest_type": "lxc"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["targets"] == [{"vmid": 201, "guest_type": "lxc"}]

    async def test_disable_clears_the_next_run(self, authenticated_client: ApiClient) -> None:
        created = authenticated_client.post("/api/v1/backup-jobs", WEEKLY).json()

        disabled = authenticated_client.post(f"/api/v1/backup-jobs/{created['id']}/disable").json()
        assert disabled["enabled"] is False
        assert disabled["next_run_at"] is None

        enabled = authenticated_client.post(f"/api/v1/backup-jobs/{created['id']}/enable").json()
        assert enabled["enabled"] is True
        assert enabled["next_run_at"] is not None

    async def test_delete(self, authenticated_client: ApiClient) -> None:
        created = authenticated_client.post("/api/v1/backup-jobs", WEEKLY).json()

        assert (
            authenticated_client.delete(f"/api/v1/backup-jobs/{created['id']}").status_code == 204
        )
        assert authenticated_client.get(f"/api/v1/backup-jobs/{created['id']}").status_code == 404


class TestPreview:
    async def test_shows_five_fire_times_and_the_resolved_targets(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(
            session_factory,
            make_guest(101, name="web"),
            make_guest(201, guest_type=GuestType.LXC, name="proxy"),
        )
        created = authenticated_client.post("/api/v1/backup-jobs", WEEKLY).json()

        body = authenticated_client.get(f"/api/v1/backup-jobs/{created['id']}/preview").json()

        assert len(body["next_fire_times"]) == 5
        assert len(set(body["next_fire_times"])) == 5, "five distinct dates, not one repeated"
        assert sorted(target["vmid"] for target in body["resolved_targets"]) == [101, 201]
        assert body["skipped"] == []

    async def test_names_targets_that_would_not_run(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A schedule that resolves to nothing must not look healthy."""
        await seed_guests(session_factory, make_guest(103, name="scratch", enabled=False))
        created = authenticated_client.post(
            "/api/v1/backup-jobs",
            {
                **WEEKLY,
                "target_selector": "include",
                "targets": [{"vmid": 103, "guest_type": "vm"}],
            },
        ).json()

        body = authenticated_client.get(f"/api/v1/backup-jobs/{created['id']}/preview").json()

        assert body["resolved_targets"] == []
        assert "not enabled for backup" in body["skipped"][0]


class TestRunNow:
    async def test_queues_a_run(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))
        created = authenticated_client.post("/api/v1/backup-jobs", WEEKLY).json()

        response = authenticated_client.post(f"/api/v1/backup-jobs/{created['id']}/run")

        assert response.status_code == 202
        run = response.json()["run"]
        assert run["status"] == "queued"
        assert run["job_name"] == "Weekly Full"
        assert run["guest_total"] == 1
        # Out-of-band, so history can tell a button press from the schedule firing.
        assert run["trigger"] == "manual"

    async def test_a_job_with_no_runnable_targets_is_refused(
        self, authenticated_client: ApiClient
    ) -> None:
        created = authenticated_client.post("/api/v1/backup-jobs", WEEKLY).json()

        response = authenticated_client.post(f"/api/v1/backup-jobs/{created['id']}/run")

        assert response.status_code == 400
        assert "resolves to no guests" in response.json()["detail"]
