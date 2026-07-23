"""Filesystem and PVE storage reporting."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from app.core.config import AgentSettings
from app.core.errors import NotFound
from app.core.logging import logger
from app.executors.pvesm import PvesmClient
from app.schemas.responses import (
    FilesystemUsageResponse,
    StorageEntryResponse,
    StorageStatusResponse,
)
from app.services.artifact_service import ArtifactService


def _statvfs_usage(path: Path) -> FilesystemUsageResponse:
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    # Used excludes root-reserved blocks, matching what `df` reports to a non-root caller.
    used = total - (stat.f_bfree * stat.f_frsize)
    return FilesystemUsageResponse(
        path=str(path),
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        used_percent=round(used / total * 100, 2) if total else 0.0,
    )


class StorageService:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        pvesm: PvesmClient,
        artifacts: ArtifactService,
    ) -> None:
        self._settings = settings
        self._pvesm = pvesm
        self._artifacts = artifacts

    async def status(self) -> StorageStatusResponse:
        root = self._settings.dump_root
        if not root.is_dir():
            raise NotFound(f"Backup directory {root} does not exist")

        usage = await asyncio.to_thread(_statvfs_usage, root)
        count, total_bytes = await asyncio.to_thread(self._artifacts.usage)

        storages: list[StorageEntryResponse] = []
        try:
            for entry in await self._pvesm.status():
                storages.append(
                    StorageEntryResponse(
                        name=entry.name,
                        type=entry.type,
                        active=entry.active,
                        total_bytes=entry.total_bytes,
                        used_bytes=entry.used_bytes,
                        available_bytes=entry.available_bytes,
                        used_percent=entry.used_percent,
                    )
                )
        except Exception:  # noqa: BLE001 - filesystem figures are still worth returning
            logger.warning("pvesm_status_unavailable", exc_info=True)

        return StorageStatusResponse(
            dump_root=usage,
            storages=storages,
            artifact_count=count,
            artifact_bytes=total_bytes,
        )

    async def free_bytes(self) -> int:
        usage = await asyncio.to_thread(_statvfs_usage, self._settings.dump_root)
        return usage.free_bytes
