"""ProxSync Backup Agent application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app import __version__
from app.api.deps import authenticate, build_container, guard_client
from app.api.routes import backup, health, restore, storage, sync, task
from app.core.config import AgentSettings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, logger

DESCRIPTION = """\
Privileged executor for Proxmox VE backup and restore operations.

This service exposes a **closed** set of operations. It does not run arbitrary commands, and
every argument is validated against the host's real state before a process is spawned.
Reachable only from the ProxSync dashboard: address allow-list, mutual TLS, and an HMAC
signature on every request.
"""


def create_app(settings: AgentSettings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.ensure_directories()
        container = app.state.container
        recovered = container.registry.load()
        pruned = container.registry.prune()
        logger.info(
            "agent_started",
            version=__version__,
            dump_root=str(resolved.dump_root),
            tasks_recovered=recovered,
            tasks_pruned=pruned,
            mtls=resolved.mtls_enabled,
            allowed_clients=resolved.allowed_client_networks,
            backup_capacity=resolved.max_concurrent_backups,
            sync_enabled=resolved.sync_enabled,
        )
        if resolved.sync_enabled and not resolved.allowed_remotes:
            logger.warning(
                "sync_remotes_unrestricted",
                detail="allowed_remotes is empty, so any remote configured in rclone.conf "
                "may be addressed — including a local one, which turns an upload into an "
                "arbitrary file read. Name the remotes you intend to use.",
            )
        if not resolved.mtls_enabled:
            logger.warning(
                "mtls_disabled",
                detail="No client CA configured; the agent is protected by HMAC and the "
                "address allow-list only. Configure tls_client_ca in production.",
            )
        yield
        logger.info("agent_stopping")

    app = FastAPI(
        title="ProxSync Backup Agent",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
        dependencies=[Depends(guard_client)],
    )
    app.state.container = build_container(resolved)
    register_exception_handlers(app)

    authenticated = [Depends(authenticate)]
    app.include_router(health.router)
    app.include_router(backup.router, dependencies=authenticated)
    app.include_router(restore.router, dependencies=authenticated)
    app.include_router(task.router, dependencies=authenticated)
    app.include_router(storage.router, dependencies=authenticated)
    app.include_router(sync.router, dependencies=authenticated)

    return app


app = create_app
"""Import target for ``uvicorn app.main:app --factory``."""
