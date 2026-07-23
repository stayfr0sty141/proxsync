"""ProxSync dashboard API application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.deps import Container, build_container
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import (
    HEADER_CORRELATION_ID,
    configure_logging,
    get_correlation_id,
    logger,
    set_correlation_id,
)
from app.repositories.audit_repository import SqlAlchemyAuditRepository
from app.repositories.refresh_token_repository import SqlAlchemyRefreshTokenRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.user_repository import SqlAlchemyUserRepository
from app.services.auth_service import AuthService
from app.services.settings_service import SettingsService

DESCRIPTION = """\
Backup management for Proxmox VE: schedules, history, retention, Google Drive replication and
guarded restores.

This service holds no privileges on the Proxmox host. Every operation that touches a guest is
delegated to the Backup Agent over mutual TLS, and the agent accepts only a fixed, validated
command vocabulary.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(
        level=resolved.log_level,
        json_output=resolved.log_json,
        # Capture is armed here but drained by `LogWriter`. With workers disabled nothing
        # drains it, so it stays off rather than filling a buffer no one empties.
        persist=resolved.log_persist_enabled and resolved.background_workers,
        persist_level=resolved.log_persist_level,
        buffer_size=resolved.log_buffer_size,
    )
    started_at = datetime.now(UTC)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        container = application.state.container
        await _startup_tasks(container)
        await _start_workers(container)
        logger.info(
            "api_started",
            version=__version__,
            environment=resolved.environment,
            database="sqlite" if resolved.is_sqlite else "postgresql",
            agent_configured=resolved.agent_configured,
            proxmox_configured=resolved.proxmox_configured,
            workers=resolved.background_workers,
        )
        yield
        await _stop_workers(container)
        await container.agent.aclose()
        await container.proxmox.aclose()
        await container.telegram.aclose()
        await container.database.dispose()
        logger.info("api_stopped")

    app = FastAPI(
        title="ProxSync",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if resolved.environment == "development" else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        root_path=resolved.root_path,
    )
    app.state.container = build_container(resolved, started_at=started_at)
    register_exception_handlers(app)

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Correlation-ID"],
            expose_headers=[HEADER_CORRELATION_ID],
        )

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = set_correlation_id(request.headers.get(HEADER_CORRELATION_ID))
        response = await call_next(request)
        response.headers[HEADER_CORRELATION_ID] = correlation_id
        return response

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(api_router)
    return app


async def _startup_tasks(container: object) -> None:
    """Seed default settings and, on a fresh install, the bootstrap administrator.

    Both are idempotent. Schema creation is *not* done here — Alembic owns the schema, and a
    service that silently created tables would mask a failed migration.
    """
    assert isinstance(container, Container)

    async with container.database.session() as session:
        settings_service = SettingsService(
            repository=SqlAlchemySettingsRepository(session),
            secret_box=container.secret_box,
        )
        seeded = await settings_service.ensure_defaults(
            timezone=container.settings.default_timezone
        )

        auth_service = AuthService(
            settings=container.settings,
            users=SqlAlchemyUserRepository(session),
            refresh_tokens=SqlAlchemyRefreshTokenRepository(session),
            audit=SqlAlchemyAuditRepository(session),
            passwords=container.passwords,
            tokens=container.tokens,
            limiter=container.login_limiter,
        )
        admin = await auth_service.ensure_bootstrap_admin()

    if seeded or admin is not None:
        logger.info(
            "startup_seed_complete",
            settings_seeded=seeded,
            bootstrap_admin=admin.username if admin else None,
            correlation_id=get_correlation_id(),
        )


async def _start_workers(container: Container) -> None:
    """Start the durable executors and policy monitors.

    The run executor starts even when the scheduler is disabled: it is what reconciles
    backups left in flight by the previous process, and skipping that would leave rows
    stuck in `running` forever.
    """
    if not container.settings.background_workers:
        logger.warning("background_workers_disabled")
        return

    # First: it is what persists everything the other workers are about to log, including
    # their recovery decisions.
    if container.settings.log_persist_enabled:
        await container.log_writer.start()
    # Before anything that can produce a notification, so a message queued during recovery is
    # delivered rather than waiting for the first idle poll.
    await container.notification_worker.start()
    # Retention performs its startup rescan before new work can arrive. The storage sampler
    # starts before the scheduler so a fresh threshold state is available before jobs fire.
    await container.retention_worker.start()
    await container.storage_sampler.start()
    # Before the backup and sync workers: a restore left running by the previous process must
    # be reconciled while it is still the only thing holding the agent's restore slot.
    await container.restore_worker.start()
    await container.runner.start()
    await container.sync_worker.start()
    if container.settings.scheduler_enabled:
        await container.scheduler.start()


async def _stop_workers(container: Container) -> None:
    if not container.settings.background_workers:
        return
    await container.scheduler.stop()
    await container.sync_worker.stop()
    await container.runner.stop()
    await container.restore_worker.stop()
    await container.retention_worker.stop()
    await container.storage_sampler.stop()
    # Last two, in reverse of their start order: the outbox drains whatever the workers above
    # queued on their way down, and the log writer persists the lines describing all of it.
    await container.notification_worker.stop()
    await container.log_writer.stop()
