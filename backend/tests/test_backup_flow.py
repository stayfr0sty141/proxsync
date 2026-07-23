"""End-to-end: a manual backup driven through the real API with the workers running.

Every other test drives one layer. This one goes request → run row → doorbell → executor →
agent → history, through the same wiring `create_app` builds in production, because the seams
between those layers are where an application of this shape actually breaks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.enums import GuestType

from .conftest import (
    ADMIN_PASSWORD,
    ApiClient,
    StubProxmoxTransport,
    install_stub_clients,
    make_guest,
    pve_guest,
    seed_guests,
)
from .test_backup_runner import ScriptedAgentTransport, archive_for

POLL_TIMEOUT_SECONDS = 10.0


@pytest.fixture
def worker_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "background_workers": True,
            # The scheduler is exercised in test_scheduler.py; leaving it off here keeps this
            # test about the run pipeline rather than about wall-clock time.
            "scheduler_enabled": False,
            "run_idle_poll_seconds": 0.05,
            "sync_idle_poll_seconds": 0.05,
            "storage_sample_interval_seconds": 0.05,
        }
    )


@pytest.fixture
def scripted() -> ScriptedAgentTransport:
    return ScriptedAgentTransport()


@pytest.fixture
def worker_client(
    worker_settings: Settings,
    prepared_database: None,
    scripted: ScriptedAgentTransport,
    proxmox_transport: StubProxmoxTransport,
) -> Iterator[ApiClient]:
    del prepared_database
    app = create_app(worker_settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        install_stub_clients(app, agent=scripted, proxmox=proxmox_transport)
        # The runner reads its poll interval from the settings table, whose floor is one
        # second. Overridden here so the test measures behaviour, not latency.
        app.state.container.runner._poll_interval = _instant  # noqa: SLF001
        app.state.container.sync_worker._poll_interval = _instant  # noqa: SLF001
        yield ApiClient(test_client)


async def _instant() -> float:
    return 0.001


def signed_in(client: ApiClient) -> ApiClient:
    client.login()
    client.post(
        "/api/v1/auth/change-password",
        {"current_password": ADMIN_PASSWORD, "new_password": "a-much-better-password-2"},
    )
    client.login(password="a-much-better-password-2")
    return client


async def wait_for_run(client: ApiClient, run_id: int, *, status: str) -> dict[str, Any]:
    """Poll the API until the run reaches a state, as a browser would."""
    deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_SECONDS
    body: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        body = client.get(f"/api/v1/runs/{run_id}").json()
        if body["status"] == status:
            return body
        await asyncio.sleep(0.02)
    pytest.fail(f"run {run_id} never reached '{status}'; last state was {body.get('status')}")


class TestManualBackupEndToEnd:
    async def test_a_backup_now_request_produces_a_history_row(
        self,
        worker_client: ApiClient,
        scripted: ScriptedAgentTransport,
        session_factory: Any,
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"), make_guest(102, name="db"))
        client = signed_in(worker_client)
        scripted.states = ["running", "success"]

        accepted = client.post(
            "/api/v1/backups/run",
            {
                "targets": [
                    {"vmid": 101, "guest_type": "vm"},
                    {"vmid": 102, "guest_type": "vm"},
                ]
            },
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run"]["id"]

        run = await wait_for_run(client, run_id, status="success")

        assert (run["guest_succeeded"], run["guest_failed"]) == (2, 0)
        assert run["duration_seconds"] is not None

        history = client.get(f"/api/v1/runs/{run_id}/backups").json()
        assert history["total"] == 2
        assert {item["filename"] for item in history["items"]} == {
            archive_for(101, GuestType.VM),
            archive_for(102, GuestType.VM),
        }
        assert all(item["status"] == "success" for item in history["items"])
        assert all(item["checksum_sha256"] for item in history["items"])

        # Two guests, two vzdumps — never more, whatever the retry logic did.
        assert len(scripted.started_backups) == 2

    async def test_a_partial_run_reports_both_outcomes(
        self,
        worker_client: ApiClient,
        scripted: ScriptedAgentTransport,
        session_factory: Any,
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"), make_guest(102, name="db"))
        client = signed_in(worker_client)
        scripted.set_states(102, ["failed"])
        scripted.error = "vzdump: no space left on device"

        run_id = client.post(
            "/api/v1/backups/run",
            {
                "targets": [
                    {"vmid": 101, "guest_type": "vm"},
                    {"vmid": 102, "guest_type": "vm"},
                ]
            },
        ).json()["run"]["id"]

        run = await wait_for_run(client, run_id, status="partial")

        assert (run["guest_succeeded"], run["guest_failed"]) == (1, 1)

        by_vmid = {
            item["vmid"]: item
            for item in client.get(f"/api/v1/runs/{run_id}/backups").json()["items"]
        }
        assert by_vmid[101]["status"] == "success"
        assert by_vmid[102]["status"] == "failed"
        assert "no space left" in by_vmid[102]["error_message"]

    async def test_the_guest_inventory_reaches_the_api_from_proxmox(
        self, worker_client: ApiClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(
            vms=[pve_guest(101, "web", tags="prod")], lxcs=[pve_guest(201, "proxy")]
        )
        client = signed_in(worker_client)

        client.post("/api/v1/guests/refresh")
        guests = client.get("/api/v1/guests").json()

        assert [item["vmid"] for item in guests["items"]] == [101, 201]
        assert guests["items"][0]["tags"] == ["prod"]
        # Not enabled by default: adding a VM must never silently widen what is backed up.
        assert all(item["backup_enabled"] is False for item in guests["items"])

    async def test_health_reports_the_workers(self, worker_client: ApiClient) -> None:
        client = signed_in(worker_client)
        deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_SECONDS
        worker_health: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            components = {
                component["name"]: component
                for component in client.get("/api/v1/health/detail").json()["components"]
            }
            worker_health = components["workers"]
            if worker_health["status"] == "ok":
                break
            await asyncio.sleep(0.05)

        assert worker_health["status"] == "ok", worker_health

    async def test_a_backup_with_upload_enabled_reaches_the_transfer_queue(
        self,
        worker_client: ApiClient,
        scripted: ScriptedAgentTransport,
        session_factory: Any,
    ) -> None:
        """The seam between the two workers: the run executor marks the artifact `pending`,
        and the sync worker picks it up without either knowing about the other."""
        await seed_guests(session_factory, make_guest(101, name="web"))
        client = signed_in(worker_client)

        client.put(
            "/api/v1/settings/gdrive",
            {"enabled": True, "remote_name": "gdrive", "folder": "proxsync/dump"},
        )

        run_id = client.post(
            "/api/v1/backups/run",
            {"targets": [{"vmid": 101, "guest_type": "vm"}], "upload": True},
        ).json()["run"]["id"]
        await wait_for_run(client, run_id, status="success")

        [backup] = client.get(f"/api/v1/runs/{run_id}/backups").json()["items"]
        assert backup["upload_status"] in {"pending", "uploading", "uploaded"}, (
            "a run that asked for an upload must leave the artifact queued for one"
        )

        deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if client.get("/api/v1/sync/tasks").json()["total"] >= 1:
                break
            await asyncio.sleep(0.05)

        tasks = client.get("/api/v1/sync/tasks").json()
        assert tasks["total"] == 1
        assert tasks["items"][0]["backup_id"] == backup["id"]
