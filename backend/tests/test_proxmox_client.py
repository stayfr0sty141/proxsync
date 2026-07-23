"""The read-only Proxmox inventory client."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.clients.proxmox_client import ProxmoxClient
from app.core.config import Settings
from app.core.errors import ConfigurationError, UpstreamError
from app.schemas.enums import GuestStatus, GuestType

from .conftest import PROXMOX_TOKEN_ID, PROXMOX_TOKEN_SECRET, StubProxmoxTransport, pve_guest


class TestAuthentication:
    async def test_sends_a_pve_api_token_header(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.route("/version", {"data": {"version": "8.3.0", "release": "8.3"}})
        await proxmox.version()

        authorization = proxmox_transport.requests[0].headers["authorization"]
        assert authorization == f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"

    async def test_refuses_to_call_without_a_token(self, settings: Settings) -> None:
        unconfigured = settings.model_copy(
            update={"proxmox_token_id": "", "proxmox_token_secret": SecretStr("")}
        )
        client = ProxmoxClient(unconfigured)
        try:
            with pytest.raises(ConfigurationError) as excinfo:
                await client.nodes()
        finally:
            await client.aclose()
        assert "PVEAuditor" in excinfo.value.detail

    async def test_write_verbs_do_not_exist(self) -> None:
        """A structural guarantee, not a convention: there is no code path that can POST."""
        for verb in ("post", "put", "patch", "delete", "_post", "_put", "_delete"):
            assert not hasattr(ProxmoxClient, verb), f"ProxmoxClient must not expose {verb}"


class TestDiscovery:
    async def test_lists_vms_and_containers_in_one_pass(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(
            vms=[pve_guest(101, "web"), pve_guest(102, "db", status="stopped")],
            lxcs=[pve_guest(201, "proxy")],
        )

        discovered = await proxmox.discover()

        assert [(item.guest_type, item.guest.vmid) for item in discovered] == [
            (GuestType.VM, 101),
            (GuestType.VM, 102),
            (GuestType.LXC, 201),
        ]
        assert discovered[1].guest.status_enum is GuestStatus.STOPPED
        assert all(item.node == "pve" for item in discovered)

    async def test_reads_from_the_configured_node(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests()
        await proxmox.discover(node="pve2")
        assert all("/nodes/pve2/" in str(r.url) for r in proxmox_transport.requests)


class TestPayloadQuirks:
    """PVE's JSON does not match its own documentation in several places."""

    async def test_tags_arrive_as_a_delimited_string(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(vms=[pve_guest(101, "web", tags="prod;web;critical")])
        guests = await proxmox.guests_of_type(GuestType.VM)
        assert guests[0].tags == ["prod", "web", "critical"]

    async def test_comma_separated_tags_are_also_accepted(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(vms=[pve_guest(101, "web", tags="prod,web")])
        guests = await proxmox.guests_of_type(GuestType.VM)
        assert guests[0].tags == ["prod", "web"]

    async def test_template_arrives_as_an_integer(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(vms=[pve_guest(101, "tpl", template=1)])
        guests = await proxmox.guests_of_type(GuestType.VM)
        assert guests[0].template is True

    async def test_a_nameless_guest_gets_a_readable_fallback(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(lxcs=[{"vmid": 201, "status": "running"}])
        guests = await proxmox.guests_of_type(GuestType.LXC)
        assert guests[0].display_name(GuestType.LXC) == "lxc-201"

    async def test_unknown_fields_are_ignored(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(vms=[pve_guest(101, "web", something_new_in_pve_9="surprise")])
        guests = await proxmox.guests_of_type(GuestType.VM)
        assert guests[0].vmid == 101

    async def test_an_unrecognised_status_becomes_unknown(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(vms=[pve_guest(101, "web", status="migrating")])
        guests = await proxmox.guests_of_type(GuestType.VM)
        assert guests[0].status_enum is GuestStatus.UNKNOWN


class TestErrorMapping:
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_a_rejected_token_is_a_configuration_error(
        self,
        proxmox: ProxmoxClient,
        proxmox_transport: StubProxmoxTransport,
        status_code: int,
    ) -> None:
        proxmox_transport.route("/nodes", {"data": None}, status_code=status_code)
        with pytest.raises(ConfigurationError) as excinfo:
            await proxmox.nodes()
        assert "PVEAuditor" in excinfo.value.detail

    async def test_a_server_error_is_an_upstream_error(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.route("/nodes", {"data": None}, status_code=500)
        with pytest.raises(UpstreamError):
            await proxmox.nodes()

    async def test_an_unreachable_host_is_an_upstream_error(self, settings: Settings) -> None:
        class Dead(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("no route to host", request=request)

        client = ProxmoxClient(
            settings, client=httpx.AsyncClient(base_url="https://pve.invalid", transport=Dead())
        )
        try:
            with pytest.raises(UpstreamError) as excinfo:
                await client.nodes()
        finally:
            await client.aclose()
        assert "Could not reach the Proxmox API" in excinfo.value.detail

    async def test_a_response_without_a_data_envelope_is_rejected(
        self, proxmox: ProxmoxClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.route("/nodes", {"unexpected": True})
        with pytest.raises(UpstreamError) as excinfo:
            await proxmox.nodes()
        assert "Unexpected Proxmox response shape" in excinfo.value.detail
