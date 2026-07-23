"""Guest inventory API models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.enums import GuestStatus, GuestType


class GuestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vmid: int
    guest_type: GuestType
    name: str
    node: str
    status: GuestStatus
    backup_enabled: bool
    tags: list[str] = []
    first_seen_at: datetime
    last_seen_at: datetime


class GuestListResponse(BaseModel):
    items: list[GuestResponse]
    total: int


class GuestUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_enabled: bool


class InventorySyncResponse(BaseModel):
    """What a refresh actually changed, rather than a bare 'ok'.

    An operator who clicks *Refresh* after adding a VM wants to see `added: 1`; a silent 200
    leaves them wondering whether the token has the right permissions.
    """

    node: str
    discovered: int
    added: int
    updated: int
    disappeared: int
    """Guests in the database that PVE no longer reports. Rows are kept — history joins
    against them — but `last_seen_at` stops advancing."""
    auto_enabled: int
    duration_ms: float
