"""Guest inventory endpoints."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.user import User
from app.schemas.enums import GuestType, UserRole

from .conftest import ApiClient, StubProxmoxTransport, make_guest, pve_guest, seed_guests


class TestListing:
    async def test_lists_the_cached_inventory(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(
            session_factory,
            make_guest(101, name="web"),
            make_guest(201, guest_type=GuestType.LXC, name="proxy", enabled=False),
        )

        response = authenticated_client.get("/api/v1/guests")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert [item["vmid"] for item in body["items"]] == [101, 201]
        assert body["items"][1]["backup_enabled"] is False

    async def test_filters_by_type(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(
            session_factory,
            make_guest(101, name="web"),
            make_guest(201, guest_type=GuestType.LXC, name="proxy"),
        )

        response = authenticated_client.get("/api/v1/guests?type=lxc")

        assert [item["vmid"] for item in response.json()["items"]] == [201]

    async def test_search_matches_a_vmid_as_well_as_a_name(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An operator who thinks in VMIDs should not have to remember the name."""
        await seed_guests(session_factory, make_guest(101, name="web"), make_guest(102, name="db"))

        by_name = authenticated_client.get("/api/v1/guests?search=web")
        by_vmid = authenticated_client.get("/api/v1/guests?search=102")

        assert [item["vmid"] for item in by_name.json()["items"]] == [101]
        assert [item["vmid"] for item in by_vmid.json()["items"]] == [102]

    async def test_unknown_guest_is_a_404(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.get("/api/v1/guests/9999")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")


class TestRefresh:
    async def test_refresh_reads_from_proxmox_and_reports_what_changed(
        self, authenticated_client: ApiClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.set_guests(vms=[pve_guest(101, "web")], lxcs=[pve_guest(201, "proxy")])

        response = authenticated_client.post("/api/v1/guests/refresh")

        assert response.status_code == 200
        body = response.json()
        assert (body["discovered"], body["added"]) == (2, 2)
        assert body["node"] == "pve"

    async def test_an_unreachable_host_is_a_502_not_a_crash(
        self, authenticated_client: ApiClient, proxmox_transport: StubProxmoxTransport
    ) -> None:
        proxmox_transport.route("/qemu", {"data": None}, status_code=500)

        response = authenticated_client.post("/api/v1/guests/refresh")

        assert response.status_code == 502
        assert response.json()["title"] == "Upstream service error"


class TestAllowList:
    async def test_admin_can_enable_a_guest(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web", enabled=False))

        response = authenticated_client.patch("/api/v1/guests/1", {"backup_enabled": True})

        assert response.status_code == 200
        assert response.json()["backup_enabled"] is True

    async def test_an_operator_may_not_widen_the_allow_list(
        self,
        client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Enabling a guest decides what may leave the host; that is an admin decision."""
        await seed_guests(session_factory, make_guest(101, name="web", enabled=False))

        client.login()
        client.post(
            "/api/v1/auth/change-password",
            {"current_password": "bootstrap-password-1", "new_password": "another-password-3"},
        )
        client.login(password="another-password-3")

        # Downgrade the signed-in account to operator, then retry.
        async with session_factory() as session:
            admin = await session.get(User, 1)
            assert admin is not None
            admin.role = UserRole.OPERATOR.value
            await session.commit()

        response = client.patch("/api/v1/guests/1", {"backup_enabled": True})

        assert response.status_code == 403
        assert "admin" in response.json()["detail"]

    async def test_unknown_field_is_rejected(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))

        response = authenticated_client.patch(
            "/api/v1/guests/1", {"backup_enabled": True, "name": "renamed"}
        )

        assert response.status_code == 422


class TestAuthentication:
    async def test_anonymous_access_is_refused(self, client: ApiClient) -> None:
        assert client.raw.get("/api/v1/guests").status_code == 401
