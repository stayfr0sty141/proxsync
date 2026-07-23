"""Guest inventory synchronisation.

The dashboard keeps a cached copy of what Proxmox reports so that history, schedules and the
UI keep working when the PVE API is briefly unreachable — a backup job must not fail to
resolve its targets because the host was rebooting.

Two rules govern the cache:

* **Nothing is ever deleted.** A guest removed from Proxmox keeps its row; `last_seen_at`
  stops advancing and the UI marks it stale. Deleting it would orphan backup history and
  make old artifacts unattributable.
* **New guests are not backed up by default.** `auto_enable_new_guests` defaults to false, so
  creating a VM never silently changes what gets backed up. The allow-list is opt-in.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from app.clients.proxmox_client import ProxmoxClient
from app.core.errors import NotFound
from app.core.events import EVENT_INVENTORY_UPDATED, EventBus
from app.core.logging import logger
from app.db.models.guest import Guest
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.schemas.enums import SettingsSection
from app.schemas.guests import InventorySyncResponse
from app.schemas.proxmox import DiscoveredGuest
from app.schemas.settings import ProxmoxSettings
from app.services.settings_service import SettingsService


class InventoryService:
    def __init__(
        self,
        *,
        guests: SqlAlchemyGuestRepository,
        proxmox: ProxmoxClient,
        settings_service: SettingsService,
        events: EventBus | None = None,
    ) -> None:
        self._guests = guests
        self._proxmox = proxmox
        self._settings_service = settings_service
        self._events = events

    async def sync(self) -> InventorySyncResponse:
        started = time.perf_counter()
        preferences = await self._preferences()
        node = preferences.node or self._proxmox.node

        discovered = await self._proxmox.discover(node=node)
        existing = {(guest.node, guest.vmid): guest for guest in await self._guests.list_all()}

        now = datetime.now(UTC)
        added = updated = auto_enabled = 0

        for item in discovered:
            current = existing.pop((item.node, item.guest.vmid), None)
            if current is None:
                await self._guests.add(
                    self._build(item, now=now, enabled=preferences.auto_enable_new_guests)
                )
                added += 1
                auto_enabled += int(preferences.auto_enable_new_guests)
            else:
                updated += int(self._apply(current, item, now=now))

        disappeared = len(existing)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        logger.info(
            "inventory_synced",
            node=node,
            discovered=len(discovered),
            added=added,
            updated=updated,
            disappeared=disappeared,
        )
        if self._events is not None and (added or updated or disappeared):
            self._events.publish(
                EVENT_INVENTORY_UPDATED,
                node=node,
                discovered=len(discovered),
                added=added,
                updated=updated,
                disappeared=disappeared,
            )

        return InventorySyncResponse(
            node=node,
            discovered=len(discovered),
            added=added,
            updated=updated,
            disappeared=disappeared,
            auto_enabled=auto_enabled,
            duration_ms=duration_ms,
        )

    async def set_backup_enabled(self, guest_id: int, *, enabled: bool) -> Guest:
        guest = await self._guests.get(guest_id)
        if guest is None:
            raise NotFound(f"No guest with id {guest_id}")
        guest.backup_enabled = enabled
        await self._guests.flush()
        logger.info(
            "guest_backup_enabled_changed",
            guest_id=guest_id,
            vmid=guest.vmid,
            enabled=enabled,
        )
        return guest

    async def get(self, guest_id: int) -> Guest:
        guest = await self._guests.get(guest_id)
        if guest is None:
            raise NotFound(f"No guest with id {guest_id}")
        return guest

    async def _preferences(self) -> ProxmoxSettings:
        section = await self._settings_service.get_section(SettingsSection.PROXMOX)
        assert isinstance(section, ProxmoxSettings)
        return section

    @staticmethod
    def _build(item: DiscoveredGuest, *, now: datetime, enabled: bool) -> Guest:
        return Guest(
            vmid=item.guest.vmid,
            guest_type=item.guest_type.value,
            name=item.guest.display_name(item.guest_type),
            node=item.node,
            status=item.guest.status_enum.value,
            backup_enabled=enabled,
            tags=item.guest.tags,
            raw=item.guest.model_dump(mode="json"),
            first_seen_at=now,
            last_seen_at=now,
        )

    @staticmethod
    def _apply(guest: Guest, item: DiscoveredGuest, *, now: datetime) -> bool:
        """Refresh a cached guest. Returns whether anything except `last_seen_at` changed.

        `backup_enabled` is never touched: it is an operator decision, not something Proxmox
        gets a vote on. A guest that is renamed or restarted keeps its allow-list state.
        """
        name = item.guest.display_name(item.guest_type)
        status = item.guest.status_enum.value
        changed = (
            guest.name != name
            or guest.status != status
            or guest.guest_type != item.guest_type.value
            or guest.tags != item.guest.tags
        )

        guest.name = name
        guest.status = status
        guest.guest_type = item.guest_type.value
        guest.tags = item.guest.tags
        guest.raw = item.guest.model_dump(mode="json")
        guest.last_seen_at = now
        return changed
