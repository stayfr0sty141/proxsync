"""Guest and storage identity checks.

A VMID is accepted only when it is numerically sane, present in the configured allow-list,
and backed by a real guest configuration on this host. Guest existence is read from
``/etc/pve/{qemu-server,lxc}/<vmid>.conf`` — authoritative, cheap, and no subprocess.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import NotFound, ValidationFailed
from app.schemas.enums import GuestType

VMID_MIN = 100
VMID_MAX = 999_999_999


@dataclass(frozen=True, slots=True)
class GuestRef:
    vmid: int
    guest_type: GuestType
    config_path: Path


class GuestLocator:
    def __init__(
        self,
        *,
        qemu_config_dir: Path,
        lxc_config_dir: Path,
        allowed_vmids: Iterable[int] = (),
    ) -> None:
        self._dirs = {GuestType.VM: qemu_config_dir, GuestType.LXC: lxc_config_dir}
        self._allowed = frozenset(allowed_vmids)

    def validate_vmid(self, vmid: int) -> int:
        if not isinstance(vmid, int) or isinstance(vmid, bool):
            raise ValidationFailed("VMID must be an integer")
        if not VMID_MIN <= vmid <= VMID_MAX:
            raise ValidationFailed(f"VMID must be between {VMID_MIN} and {VMID_MAX}")
        if self._allowed and vmid not in self._allowed:
            raise ValidationFailed(f"VMID {vmid} is not in this agent's allow-list")
        return vmid

    def config_path(self, vmid: int, guest_type: GuestType) -> Path:
        return self._dirs[guest_type] / f"{vmid}.conf"

    def find(self, vmid: int) -> GuestRef | None:
        """Locate a guest by id without knowing its type."""
        for guest_type in (GuestType.VM, GuestType.LXC):
            path = self.config_path(vmid, guest_type)
            if path.is_file():
                return GuestRef(vmid=vmid, guest_type=guest_type, config_path=path)
        return None

    def require(self, vmid: int, guest_type: GuestType) -> GuestRef:
        """Validate and resolve a guest, asserting it is of the expected type."""
        self.validate_vmid(vmid)
        found = self.find(vmid)
        if found is None:
            raise NotFound(f"No VM or container with id {vmid} exists on this host")
        if found.guest_type is not guest_type:
            raise ValidationFailed(
                f"Guest {vmid} is a {found.guest_type.value}, not a {guest_type.value}"
            )
        return found

    def require_absent(self, vmid: int) -> None:
        """Assert a VMID is free — used by restores that must not overwrite."""
        self.validate_vmid(vmid)
        found = self.find(vmid)
        if found is not None:
            raise ValidationFailed(
                f"VMID {vmid} is already used by a {found.guest_type.value}; "
                "set overwrite to replace it"
            )


class StorageValidator:
    """Validates storage identifiers against ``pvesm status``, with a short TTL cache.

    ``allowed`` narrows the result further; an empty allow-list means "anything pvesm reports".
    """

    def __init__(
        self,
        *,
        list_storages: object,
        allowed: Iterable[str] = (),
        ttl_seconds: int = 30,
        verify_with_pvesm: bool = True,
    ) -> None:
        # ``list_storages`` is an async callable returning the live storage ids. It is injected
        # rather than imported so tests do not need a Proxmox host.
        self._list_storages = list_storages
        self._allowed = frozenset(allowed)
        self._ttl = ttl_seconds
        self._verify = verify_with_pvesm
        self._cache: frozenset[str] | None = None
        self._cached_at = 0.0

    async def _live_storages(self) -> frozenset[str]:
        now = time.monotonic()
        if self._cache is not None and now - self._cached_at < self._ttl:
            return self._cache
        names = await self._list_storages()  # type: ignore[operator]
        self._cache = frozenset(names)
        self._cached_at = now
        return self._cache

    def invalidate(self) -> None:
        self._cache = None

    async def require(self, storage: str) -> str:
        if not storage or not storage.strip():
            raise ValidationFailed("Storage identifier must not be empty")
        # PVE storage ids: letters, digits, dot, dash, underscore.
        if not all(char.isalnum() or char in "._-" for char in storage):
            raise ValidationFailed(f"Storage identifier '{storage}' contains illegal characters")
        if storage.startswith("-"):
            raise ValidationFailed("Storage identifier must not start with '-'")

        if self._allowed and storage not in self._allowed:
            raise ValidationFailed(f"Storage '{storage}' is not in this agent's allow-list")

        if self._verify:
            live = await self._live_storages()
            if storage not in live:
                raise ValidationFailed(
                    f"Storage '{storage}' is not configured on this host "
                    f"(known: {', '.join(sorted(live)) or 'none'})"
                )
        return storage
