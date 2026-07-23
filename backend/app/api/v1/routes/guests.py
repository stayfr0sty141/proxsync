"""Guest inventory endpoints.

Reading the inventory is a viewer action. Changing `backup_enabled` is an **admin** action:
it is the allow-list that decides what may ever be copied off the host, so widening it is a
policy change, not an operational one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import (
    AuditRepositoryDep,
    GuestRepositoryDep,
    InventoryServiceDep,
    RequireAdmin,
    RequireOperator,
    RequireViewer,
)
from app.api.mappers import guest_to_response
from app.schemas.enums import AuditAction, GuestStatus, GuestType
from app.schemas.guests import (
    GuestListResponse,
    GuestResponse,
    GuestUpdateRequest,
    InventorySyncResponse,
)

router = APIRouter(prefix="/guests", tags=["guests"])


@router.get("", summary="List guests")
async def list_guests(
    guests: GuestRepositoryDep,
    user: RequireViewer,
    guest_type: Annotated[GuestType | None, Query(alias="type")] = None,
    status: GuestStatus | None = None,
    backup_enabled: bool | None = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
) -> GuestListResponse:
    del user
    rows = await guests.search(
        guest_type=guest_type,
        status=status,
        backup_enabled=backup_enabled,
        query=search,
    )
    return GuestListResponse(items=[guest_to_response(guest) for guest in rows], total=len(rows))


@router.post("/refresh", summary="Re-read the inventory from Proxmox")
async def refresh(service: InventoryServiceDep, user: RequireOperator) -> InventorySyncResponse:
    del user
    return await service.sync()


@router.get("/{guest_id}", summary="One guest")
async def get_guest(
    guest_id: int, service: InventoryServiceDep, user: RequireViewer
) -> GuestResponse:
    del user
    return guest_to_response(await service.get(guest_id))


@router.patch("/{guest_id}", summary="Enable or disable a guest for backup")
async def update_guest(
    guest_id: int,
    payload: GuestUpdateRequest,
    service: InventoryServiceDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
) -> GuestResponse:
    guest = await service.set_backup_enabled(guest_id, enabled=payload.backup_enabled)
    await audit.record(
        action=AuditAction.SETTINGS_CHANGED,
        user_id=user.id,
        username=user.username,
        resource_type="guest",
        resource_id=str(guest_id),
        detail={
            "vmid": guest.vmid,
            "guest_type": guest.guest_type,
            "backup_enabled": payload.backup_enabled,
        },
    )
    return guest_to_response(guest)
