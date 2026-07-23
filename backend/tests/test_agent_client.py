"""AgentClient: request signing, retries, circuit breaking and error mapping.

The signature test re-implements the agent's verification rather than trusting the client's
own helper, so a change on either side of the contract fails here.
"""

from __future__ import annotations

import httpx
import pytest

from app.clients.agent_client import AgentClient
from app.clients.circuit_breaker import CircuitState
from app.core.config import Settings
from app.core.errors import AgentError, AgentUnavailable, ConfigurationError
from app.schemas.enums import BackupMode, Compression, GuestType
from tests.conftest import (
    StubAgentTransport,
    agent_health_payload,
    verify_agent_signature,
)


@pytest.fixture
def transport() -> StubAgentTransport:
    return StubAgentTransport()


@pytest.fixture
def agent(settings: Settings, transport: StubAgentTransport) -> AgentClient:
    client = httpx.AsyncClient(base_url=settings.agent_base_url, transport=transport)
    return AgentClient(settings, client=client)


class TestSigning:
    async def test_get_request_signature_verifies(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(200, agent_health_payload())
        await agent.health()

        verify_agent_signature(transport.requests[0])

    async def test_post_body_is_covered_by_the_signature(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(
            202,
            {
                "task_id": "abc",
                "kind": "backup",
                "state": "queued",
                "created_at": "2026-07-22T01:00:00Z",
            },
        )
        await agent.start_backup(
            vmid=101,
            guest_type=GuestType.VM,
            mode=BackupMode.SNAPSHOT,
            compression=Compression.ZSTD,
            storage="backup-hdd",
        )

        recorded = transport.requests[0]
        verify_agent_signature(recorded)
        assert recorded.json_body["vmid"] == 101
        assert recorded.json_body["guest_type"] == "vm"

    async def test_query_string_is_covered_by_the_signature(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(200, [])
        await agent.list_backups(vmid=104, guest_type=GuestType.LXC)

        recorded = transport.requests[0]
        verify_agent_signature(recorded)
        assert "vmid=104" in str(recorded.url)

    async def test_nonces_are_unique_per_request(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(200, agent_health_payload())
        transport.queue(200, agent_health_payload())
        await agent.health()
        await agent.health()

        nonces = {request.headers["x-proxsync-nonce"] for request in transport.requests}
        assert len(nonces) == 2

    async def test_key_id_is_sent(self, agent: AgentClient, transport: StubAgentTransport) -> None:
        transport.queue(200, agent_health_payload())
        await agent.health()
        assert transport.requests[0].headers["x-proxsync-key"] == "proxsync-dashboard"

    async def test_a_different_secret_would_not_verify(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(200, agent_health_payload())
        await agent.health()

        with pytest.raises(AssertionError):
            verify_agent_signature(transport.requests[0], secret="wrong-secret")

    async def test_missing_secret_refuses_to_send(self, settings: Settings) -> None:
        from pydantic import SecretStr

        settings.agent_hmac_secret = SecretStr("")
        client = AgentClient(settings, client=httpx.AsyncClient(transport=StubAgentTransport()))

        with pytest.raises(ConfigurationError, match="HMAC_SECRET"):
            await client.health()


class TestResponses:
    async def test_parses_storage_status_and_signs_the_endpoint(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(
            200,
            {
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
            },
        )

        status = await agent.storage_status()

        assert status.dump_root.free_bytes == 350
        assert status.artifact_count == 4
        assert status.storages[0].name == "backup-hdd"
        assert transport.requests[0].url.path == "/storage/status"
        verify_agent_signature(transport.requests[0])

    async def test_parses_a_task(self, agent: AgentClient, transport: StubAgentTransport) -> None:
        transport.queue(
            200,
            {
                "task_id": "t1",
                "kind": "backup",
                "state": "running",
                "created_at": "2026-07-22T01:00:00Z",
                "progress": {"percent": 42.0, "bytes_done": 100},
                "meta": {"vmid": 101},
            },
        )
        task = await agent.get_task("t1")

        assert task.state == "running"
        assert task.progress.percent == 42.0
        assert task.is_terminal is False

    async def test_interrupted_is_terminal_but_not_success(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(
            200,
            {
                "task_id": "t1",
                "kind": "backup",
                "state": "interrupted",
                "created_at": "2026-07-22T01:00:00Z",
            },
        )
        task = await agent.get_task("t1")

        assert task.is_terminal
        assert not task.succeeded

    async def test_parses_artifacts(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(
            200,
            [
                {
                    "filename": "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
                    "path": "/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
                    "vmid": 101,
                    "guest_type": "vm",
                    "size_bytes": 8589934592,
                    "created_at": "2026-07-19T01:00:04+07:00",
                    "modified_at": "2026-07-19T01:06:16Z",
                    "compression": "zstd",
                    "checksum_sha256": "a" * 64,
                }
            ],
        )
        artifacts = await agent.list_backups()

        assert len(artifacts) == 1
        assert artifacts[0].vmid == 101
        assert artifacts[0].guest_type is GuestType.VM

    async def test_unknown_response_fields_are_ignored(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        """A newer agent must not break an older dashboard."""
        transport.queue(200, {**agent_health_payload(), "brand_new_field": 123})
        assert (await agent.health()).status == "ok"


class TestErrorMapping:
    @pytest.mark.parametrize("status_code", [400, 404, 409, 423, 507])
    async def test_agent_rejections_become_agent_errors(
        self, agent: AgentClient, transport: StubAgentTransport, status_code: int
    ) -> None:
        transport.queue(status_code, {"detail": "vmid 999 is not in the allow-list"})

        with pytest.raises(AgentError) as exc_info:
            await agent.health()

        assert exc_info.value.agent_status == status_code
        assert "999" in exc_info.value.detail

    async def test_authentication_failure_gets_an_actionable_message(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(401, {"detail": "Invalid request signature"})

        with pytest.raises(AgentError, match="clocks are synchronised"):
            await agent.health()

    async def test_transport_failure_becomes_unavailable(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.raise_on_request = httpx.ConnectError("connection refused")

        with pytest.raises(AgentUnavailable, match="Could not reach"):
            await agent.health()


class TestRetriesAndCircuit:
    async def test_get_is_retried(self, settings: Settings, transport: StubAgentTransport) -> None:
        settings.agent_max_retries = 2
        client = AgentClient(
            settings,
            client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=transport),
        )
        transport.raise_on_request = httpx.ConnectError("refused")

        with pytest.raises(AgentUnavailable):
            await client.health()

        assert len(transport.requests) == 3  # initial attempt plus two retries

    async def test_post_is_not_retried(
        self, settings: Settings, transport: StubAgentTransport
    ) -> None:
        """Retrying a backup start would launch a second vzdump."""
        settings.agent_max_retries = 3
        client = AgentClient(
            settings,
            client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=transport),
        )
        transport.raise_on_request = httpx.ConnectError("refused")

        with pytest.raises(AgentUnavailable):
            await client.start_backup(
                vmid=101,
                guest_type=GuestType.VM,
                mode=BackupMode.SNAPSHOT,
                compression=Compression.ZSTD,
                storage="backup-hdd",
            )

        assert len(transport.requests) == 1

    async def test_each_retry_is_freshly_signed(
        self, settings: Settings, transport: StubAgentTransport
    ) -> None:
        settings.agent_max_retries = 2
        client = AgentClient(
            settings,
            client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=transport),
        )
        transport.raise_on_request = httpx.ConnectError("refused")

        with pytest.raises(AgentUnavailable):
            await client.health()

        nonces = {request.headers["x-proxsync-nonce"] for request in transport.requests}
        assert len(nonces) == len(transport.requests)
        for request in transport.requests:
            verify_agent_signature(request)

    async def test_circuit_opens_and_short_circuits(
        self, settings: Settings, transport: StubAgentTransport
    ) -> None:
        settings.agent_max_retries = 0
        settings.agent_circuit_failure_threshold = 2
        client = AgentClient(
            settings,
            client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=transport),
        )
        transport.raise_on_request = httpx.ConnectError("refused")

        for _ in range(2):
            with pytest.raises(AgentUnavailable):
                await client.health()

        assert client.circuit_state is CircuitState.OPEN
        attempts_before = len(transport.requests)

        with pytest.raises(AgentUnavailable, match="requests are paused"):
            await client.health()

        # The short-circuited call never reached the transport.
        assert len(transport.requests) == attempts_before

    async def test_success_closes_the_circuit(
        self, agent: AgentClient, transport: StubAgentTransport
    ) -> None:
        transport.queue(200, agent_health_payload())
        await agent.health()
        assert agent.circuit_state is CircuitState.CLOSED
