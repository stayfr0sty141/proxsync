"""Composition root and FastAPI dependencies.

Every collaborator is constructed once in :func:`build_container` and injected. Nothing in
the application reaches for a module-level singleton, which is what makes the services
testable with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request

from app.core.concurrency import SlotManager
from app.core.config import AgentSettings
from app.core.logging import set_correlation_id
from app.core.security import (
    HEADER_CORRELATION_ID,
    NonceCache,
    RequestAuthenticator,
    assert_client_allowed,
)
from app.executors.base import ProcessRunner
from app.executors.pvesm import PvesmClient
from app.services.artifact_service import ArtifactService
from app.services.backup_service import BACKUP_SLOT, BackupService
from app.services.restore_service import RESTORE_SLOT, RestoreService
from app.services.storage_service import StorageService
from app.services.sync_service import SYNC_SLOT, SyncService
from app.tasks.models import utcnow
from app.tasks.registry import TaskRegistry
from app.validators.identifiers import GuestLocator, StorageValidator


@dataclass(slots=True)
class Container:
    settings: AgentSettings
    registry: TaskRegistry
    runner: ProcessRunner
    slots: SlotManager
    authenticator: RequestAuthenticator
    backup_service: BackupService
    restore_service: RestoreService
    artifact_service: ArtifactService
    storage_service: StorageService
    sync_service: SyncService
    started_at: datetime


def build_container(settings: AgentSettings) -> Container:
    runner = ProcessRunner(cancel_grace_seconds=settings.cancel_grace_seconds)
    registry = TaskRegistry(
        journal_dir=settings.task_journal_dir,
        log_dir=settings.task_log_dir,
        retention_hours=settings.task_retention_hours,
    )
    slots = SlotManager(
        {
            BACKUP_SLOT: settings.max_concurrent_backups,
            RESTORE_SLOT: settings.max_concurrent_restores,
            SYNC_SLOT: settings.max_concurrent_syncs,
        }
    )
    guests = GuestLocator(
        qemu_config_dir=settings.qemu_config_dir,
        lxc_config_dir=settings.lxc_config_dir,
        allowed_vmids=settings.allowed_vmids,
    )
    pvesm = PvesmClient(
        runner=runner,
        pvesm_bin=settings.pvesm_bin,
        timeout_seconds=settings.command_timeout_seconds,
    )
    backup_storages = StorageValidator(
        list_storages=pvesm.storage_names,
        allowed=settings.allowed_backup_storages,
        ttl_seconds=settings.storage_cache_ttl_seconds,
        verify_with_pvesm=settings.verify_storage_with_pvesm,
    )
    restore_storages = StorageValidator(
        list_storages=pvesm.storage_names,
        allowed=settings.allowed_restore_storages,
        ttl_seconds=settings.storage_cache_ttl_seconds,
        verify_with_pvesm=settings.verify_storage_with_pvesm,
    )
    artifacts = ArtifactService(settings)

    return Container(
        settings=settings,
        registry=registry,
        runner=runner,
        slots=slots,
        authenticator=RequestAuthenticator(
            key_id=settings.api_key_id,
            secret=settings.hmac_secret.get_secret_value(),
            window_seconds=settings.signature_window_seconds,
            nonce_cache=NonceCache(
                ttl_seconds=settings.signature_window_seconds * 2,
                max_size=settings.nonce_cache_size,
            ),
        ),
        backup_service=BackupService(
            settings=settings,
            registry=registry,
            runner=runner,
            guests=guests,
            storages=backup_storages,
            slots=slots,
        ),
        restore_service=RestoreService(
            settings=settings,
            registry=registry,
            runner=runner,
            guests=guests,
            storages=restore_storages,
            artifacts=artifacts,
            slots=slots,
        ),
        artifact_service=artifacts,
        storage_service=StorageService(settings=settings, pvesm=pvesm, artifacts=artifacts),
        sync_service=SyncService(
            settings=settings,
            registry=registry,
            runner=runner,
            artifacts=artifacts,
            slots=slots,
        ),
        started_at=utcnow(),
    )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def guard_client(request: Request) -> None:
    """Address allow-list, applied to *every* route including ``/health``.

    Also resets the per-request correlation id, so a value from a previous request on the same
    worker can never leak into this one's logs.
    """
    container = get_container(request)
    assert_client_allowed(
        request.client.host if request.client else None, container.settings.client_networks
    )
    set_correlation_id(request.headers.get(HEADER_CORRELATION_ID))


async def authenticate(request: Request) -> None:
    """HMAC signature verification. Applied to every route except ``/health``."""
    container = get_container(request)

    # Starlette caches the body, so the route's own model parsing re-uses this read.
    body = await request.body()
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"

    container.authenticator.verify(
        method=request.method,
        path=path,
        headers=dict(request.headers),
        body=body,
    )


ContainerDep = Annotated[Container, Depends(get_container)]
