"""Health endpoint.

Unauthenticated but still address-filtered, and deliberately sparse on failure detail: it
exists so the dashboard can show an "agent online" pill and so systemd can watchdog the
service, not to describe the host to a stranger.
"""

from __future__ import annotations

import os
import socket

from fastapi import APIRouter

from app import __version__
from app.api.deps import ContainerDep
from app.schemas.enums import TaskState
from app.schemas.responses import HealthResponse, SlotResponse
from app.tasks.models import utcnow

router = APIRouter(tags=["health"])


@router.get("/health", summary="Agent health")
async def health(container: ContainerDep) -> HealthResponse:
    settings = container.settings
    dump_root = settings.dump_root

    binaries = {
        "vzdump": settings.vzdump_bin.is_file(),
        "qmrestore": settings.qmrestore_bin.is_file(),
        "qm": settings.qm_bin.is_file(),
        "pct": settings.pct_bin.is_file(),
        "pvesm": settings.pvesm_bin.is_file(),
    }
    # Only required when sync is switched on: an agent doing local backups only should not
    # report itself degraded for missing a tool it will never run.
    if settings.sync_enabled:
        binaries["rclone"] = settings.rclone_bin.is_file()

    writable = dump_root.is_dir() and os.access(dump_root, os.W_OK)
    active = len(container.registry.list(state=TaskState.RUNNING))

    return HealthResponse(
        status="ok" if writable and all(binaries.values()) else "degraded",
        version=__version__,
        started_at=container.started_at,
        uptime_seconds=round((utcnow() - container.started_at).total_seconds(), 1),
        hostname=socket.gethostname(),
        dump_root=str(dump_root),
        dump_root_writable=writable,
        binaries=binaries,
        slots=[
            SlotResponse(name=slot.name, in_use=slot.in_use, capacity=slot.capacity)
            for slot in container.slots.state()
        ],
        active_tasks=active,
        mtls_enabled=settings.mtls_enabled,
    )
