"""Guest inventory synchronisation."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.proxmox_client import ProxmoxClient
from app.core.crypto import SecretBox
from app.core.errors import NotFound
from app.core.events import EventBus
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import GuestStatus, GuestType, SettingsSection
from app.schemas.guests import InventorySyncResponse
from app.services.inventory_service import InventoryService
from app.services.settings_service import SettingsService

from .conftest import SECRET_KEY, StubProxmoxTransport, make_guest, pve_guest, seed_guests


class ServiceFactory(Protocol):
    def __call__(
        self, session: AsyncSession, events: EventBus | None = None
    ) -> InventoryService: ...


@pytest.fixture
def build_service(
    session_factory: async_sessionmaker[AsyncSession],
    proxmox: ProxmoxClient,
) -> ServiceFactory:
    del session_factory

    def factory(session: AsyncSession, events: EventBus | None = None) -> InventoryService:
        return InventoryService(
            guests=SqlAlchemyGuestRepository(session),
            proxmox=proxmox,
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=SecretBox(SECRET_KEY),
            ),
            events=events,
        )

    return factory


async def _sync(
    session_factory: async_sessionmaker[AsyncSession],
    build_service: ServiceFactory,
    *,
    events: EventBus | None = None,
) -> InventorySyncResponse:
    async with session_factory() as session:
        outcome = await build_service(session, events).sync()
        await session.commit()
        return outcome


class TestFirstSync:
    async def test_discovers_and_records_guests(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        proxmox_transport.set_guests(
            vms=[pve_guest(101, "web", tags="prod")],
            lxcs=[pve_guest(201, "proxy", status="stopped")],
        )

        outcome = await _sync(session_factory, build_service)

        assert (outcome.discovered, outcome.added, outcome.updated) == (2, 2, 0)
        async with session_factory() as session:
            guests = await SqlAlchemyGuestRepository(session).list_all()
        assert [(g.vmid, g.guest_type, g.name) for g in guests] == [
            (101, "vm", "web"),
            (201, "lxc", "proxy"),
        ]
        assert guests[0].tags == ["prod"]
        assert guests[1].status == GuestStatus.STOPPED.value

    async def test_new_guests_are_not_backed_up_by_default(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        """Adding a VM to the host must never silently change what gets backed up."""
        proxmox_transport.set_guests(vms=[pve_guest(101, "web")])

        outcome = await _sync(session_factory, build_service)

        assert outcome.auto_enabled == 0
        async with session_factory() as session:
            guest = await SqlAlchemyGuestRepository(session).get_by_vmid(101, GuestType.VM)
        assert guest is not None
        assert guest.backup_enabled is False

    async def test_auto_enable_can_be_switched_on(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        async with session_factory() as session:
            service = SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=SecretBox(SECRET_KEY),
            )
            await service.ensure_defaults()
            await service.update_section(SettingsSection.PROXMOX, {"auto_enable_new_guests": True})
            await session.commit()

        proxmox_transport.set_guests(vms=[pve_guest(101, "web")])
        outcome = await _sync(session_factory, build_service)

        assert outcome.auto_enabled == 1
        async with session_factory() as session:
            guest = await SqlAlchemyGuestRepository(session).get_by_vmid(101, GuestType.VM)
        assert guest is not None
        assert guest.backup_enabled is True


class TestResync:
    async def test_updates_a_renamed_guest_without_duplicating_it(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))

        proxmox_transport.set_guests(vms=[pve_guest(101, "web-renamed", status="stopped")])
        outcome = await _sync(session_factory, build_service)

        assert (outcome.added, outcome.updated) == (0, 1)
        async with session_factory() as session:
            guests = await SqlAlchemyGuestRepository(session).list_all()
        assert len(guests) == 1
        assert guests[0].name == "web-renamed"
        assert guests[0].status == GuestStatus.STOPPED.value

    async def test_the_allow_list_survives_a_resync(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        """`backup_enabled` is an operator decision; Proxmox does not get a vote."""
        await seed_guests(
            session_factory,
            make_guest(101, name="web", enabled=True),
            make_guest(102, name="db", enabled=False),
        )

        proxmox_transport.set_guests(vms=[pve_guest(101, "web"), pve_guest(102, "db")])
        await _sync(session_factory, build_service)

        async with session_factory() as session:
            repository = SqlAlchemyGuestRepository(session)
            web = await repository.get_by_vmid(101, GuestType.VM)
            db = await repository.get_by_vmid(102, GuestType.VM)
        assert web is not None and db is not None
        assert (web.backup_enabled, db.backup_enabled) == (True, False)

    async def test_a_guest_removed_from_proxmox_is_kept_not_deleted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        """Deleting the row would orphan its backup history."""
        await seed_guests(session_factory, make_guest(101, name="web"))
        before = await _seen_at(session_factory, 101)

        proxmox_transport.set_guests(vms=[])
        outcome = await _sync(session_factory, build_service)

        assert outcome.disappeared == 1
        async with session_factory() as session:
            guests = await SqlAlchemyGuestRepository(session).list_all()
        assert len(guests) == 1, "the row must survive"
        assert guests[0].last_seen_at == before, "last_seen_at must stop advancing"

    async def test_an_unchanged_guest_is_not_counted_as_updated(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))
        proxmox_transport.set_guests(vms=[pve_guest(101, "web")])

        outcome = await _sync(session_factory, build_service)

        assert (outcome.added, outcome.updated, outcome.disappeared) == (0, 0, 0)


class TestEvents:
    async def test_a_change_is_published(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        bus = EventBus()
        proxmox_transport.set_guests(vms=[pve_guest(101, "web")])

        async with bus.subscribe() as queue:
            await _sync(session_factory, build_service, events=bus)
            event = queue.get_nowait()

        assert event.type == "inventory.updated"
        assert event.data["added"] == 1

    async def test_a_no_op_sync_publishes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        proxmox_transport: StubProxmoxTransport,
        build_service: ServiceFactory,
    ) -> None:
        bus = EventBus()
        proxmox_transport.set_guests()

        async with bus.subscribe() as queue:
            await _sync(session_factory, build_service, events=bus)

        assert queue.empty()


class TestAllowList:
    async def test_toggle(
        self, session_factory: async_sessionmaker[AsyncSession], build_service: ServiceFactory
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web", enabled=False))

        async with session_factory() as session:
            service = build_service(session)
            guest = await service.set_backup_enabled(1, enabled=True)
            await session.commit()

        assert guest.backup_enabled is True

    async def test_unknown_guest_is_a_404(
        self, session_factory: async_sessionmaker[AsyncSession], build_service: ServiceFactory
    ) -> None:
        async with session_factory() as session:
            service = build_service(session)
            with pytest.raises(NotFound):
                await service.set_backup_enabled(9999, enabled=True)


async def _seen_at(session_factory: async_sessionmaker[AsyncSession], vmid: int) -> datetime:
    async with session_factory() as session:
        guest = await SqlAlchemyGuestRepository(session).get_by_vmid(vmid, GuestType.VM)
    assert guest is not None
    return guest.last_seen_at
