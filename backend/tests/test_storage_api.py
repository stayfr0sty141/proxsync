"""HTTP contract tests for the read-only storage dashboard endpoints."""

from __future__ import annotations

from tests.conftest import ApiClient, StubAgentTransport

STORAGE = "/api/v1/storage"


def local_status_payload() -> dict[str, object]:
    return {
        "dump_root": {
            "path": "/mnt/backup-hdd/dump",
            "total_bytes": 1_000,
            "used_bytes": 600,
            "free_bytes": 350,
            "used_percent": 60.0,
        },
        "storages": [
            {
                "name": "backup-hdd",
                "type": "dir",
                "active": True,
                "total_bytes": 1_000,
                "used_bytes": 600,
                "available_bytes": 350,
                "used_percent": 60.0,
            }
        ],
        "artifact_count": 4,
        "artifact_bytes": 500,
    }


class TestStorageApi:
    async def test_all_viewer_endpoints_are_mounted(
        self,
        authenticated_client: ApiClient,
        agent_transport: StubAgentTransport,
    ) -> None:
        agent_transport.queue(200, local_status_payload())

        live = authenticated_client.get(STORAGE)
        history = authenticated_client.get(f"{STORAGE}/history")
        forecast = authenticated_client.get(f"{STORAGE}/forecast")
        by_guest = authenticated_client.get(f"{STORAGE}/by-guest")

        assert live.status_code == 200
        assert live.json()["local"]["used_percent"] == 65.0
        assert history.status_code == 200
        assert history.json() == {"days": 30, "items": []}
        assert forecast.status_code == 200
        assert forecast.json()["sample_count"] == 0
        assert by_guest.status_code == 200
        assert by_guest.json() == {"items": [], "total_bytes": 0}

    async def test_anonymous_access_is_refused(self, client: ApiClient) -> None:
        for path in (
            STORAGE,
            f"{STORAGE}/history",
            f"{STORAGE}/forecast",
            f"{STORAGE}/by-guest",
        ):
            assert client.raw.get(path).status_code == 401

    async def test_history_range_is_validated(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.get(f"{STORAGE}/history?days=0").status_code == 422
        assert authenticated_client.get(f"{STORAGE}/history?days=366").status_code == 422
