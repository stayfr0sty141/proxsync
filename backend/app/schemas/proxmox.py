"""Typed mirrors of the Proxmox VE API responses the dashboard reads.

Only the fields ProxSync actually uses are declared; `extra="ignore"` keeps a PVE upgrade
that adds fields from breaking the parse. The full payload is kept separately in
`Guest.raw` for diagnostics, so nothing is silently lost.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.enums import GuestStatus, GuestType


class PveGuest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vmid: int
    name: str = ""
    status: str = "unknown"
    template: bool = False
    tags: list[str] = []
    maxdisk: int | None = None
    maxmem: int | None = None
    uptime: int | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, value: Any) -> Any:
        """PVE serialises tags as one delimited string; the delimiter has varied by release."""
        if isinstance(value, str):
            return [tag for tag in value.replace(",", ";").split(";") if tag.strip()]
        return value

    @field_validator("template", mode="before")
    @classmethod
    def _coerce_template(cls, value: Any) -> Any:
        # PVE sends 0/1, not a JSON boolean.
        return bool(int(value)) if isinstance(value, (int, str)) and str(value).isdigit() else value

    @property
    def status_enum(self) -> GuestStatus:
        try:
            return GuestStatus(self.status)
        except ValueError:
            return GuestStatus.UNKNOWN

    def display_name(self, guest_type: GuestType) -> str:
        """LXC containers report `hostname`, VMs report `name`; either may be blank."""
        return self.name or f"{guest_type.value}-{self.vmid}"


class PveNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    node: str
    status: str = "unknown"
    uptime: int | None = None


class PveVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = ""
    release: str = ""
    repoid: str = ""


class DiscoveredGuest(BaseModel):
    """A guest as the inventory service sees it: PVE payload plus the type it was found under."""

    guest_type: GuestType
    node: str
    guest: PveGuest
