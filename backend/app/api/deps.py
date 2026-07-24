"""Composition root and request-scoped dependencies.

**Transaction boundary.** The session dependency commits on success *and* on expected domain
errors (`AppError` with a 4xx status). That is deliberate: a failed login must still persist
its audit row and its lockout counter, and rolling those back would make brute-force
protection unenforceable. Unexpected errors (5xx, anything unhandled) always roll back.

The rule this places on services: do not leave partially-applied state before raising a 4xx.
Validate first, mutate second.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.agent_client import AgentClient
from app.clients.proxmox_client import ProxmoxClient
from app.clients.telegram_client import TelegramClient
from app.core.config import Settings
from app.core.crypto import SecretBox, derive_jwt_key
from app.core.errors import AppError, AuthenticationFailed, CsrfFailed, PermissionDenied
from app.core.events import EventBus
from app.core.rate_limit import SlidingWindowLimiter
from app.core.retention_guard import RetentionGuard
from app.core.security import PasswordService, TokenService, csrf_tokens_match
from app.db.models.user import User
from app.db.session import Database
from app.repositories.audit_repository import SqlAlchemyAuditRepository
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.backup_job_repository import SqlAlchemyBackupJobRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.log_repository import SqlAlchemyLogRepository
from app.repositories.notification_repository import SqlAlchemyNotificationRepository
from app.repositories.refresh_token_repository import SqlAlchemyRefreshTokenRepository
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.repositories.retention_repository import SqlAlchemyRetentionRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.storage_snapshot_repository import SqlAlchemyStorageSnapshotRepository
from app.repositories.sync_task_repository import SqlAlchemySyncTaskRepository
from app.repositories.user_repository import SqlAlchemyUserRepository
from app.schemas.enums import SettingsSection, UserRole
from app.schemas.settings import SectionModel
from app.services.auth_service import AuthService, RequestContext
from app.services.backup_service import BackupService
from app.services.dashboard_service import DashboardService
from app.services.health_service import HealthService
from app.services.inventory_service import InventoryService
from app.services.log_service import LogService
from app.services.notification_service import NotificationGateway, NotificationService
from app.services.restore_service import RestoreService
from app.services.retention_service import RetentionService
from app.services.schedule_service import ScheduleService
from app.services.settings_service import SettingsService
from app.services.storage_service import StorageService
from app.services.sync_service import SyncService
from app.workers.backup_runner import BackupRunner
from app.workers.log_writer import LogWriter
from app.workers.notification_worker import NotificationWorker
from app.workers.restore_worker import RestoreWorker
from app.workers.retention_worker import RetentionWorker
from app.workers.scheduler import SchedulerWorker
from app.workers.storage_sampler import StorageSampler
from app.workers.sync_worker import SyncWorker

REFRESH_COOKIE = "proxsync_refresh"  # noqa: S105 - a cookie name
CSRF_COOKIE = "proxsync_csrf"  # noqa: S105
CSRF_HEADER = "X-CSRF-Token"  # noqa: S105


@dataclass(slots=True)
class Container:
    settings: Settings
    database: Database
    passwords: PasswordService
    tokens: TokenService
    secret_box: SecretBox
    agent: AgentClient
    proxmox: ProxmoxClient
    telegram: TelegramClient
    events: EventBus
    login_limiter: SlidingWindowLimiter
    started_at: datetime
    runner: BackupRunner
    scheduler: SchedulerWorker
    sync_worker: SyncWorker
    restore_worker: RestoreWorker
    retention_worker: RetentionWorker
    storage_sampler: StorageSampler
    notification_worker: NotificationWorker
    log_writer: LogWriter
    retention_guard: RetentionGuard


@dataclass(frozen=True, slots=True)
class DetachedSettingsReader:
    """Read settings in a short transaction before a service performs network I/O.

    Request-scoped SQLAlchemy sessions are convenient for ordinary CRUD, but using one for
    ``GET /storage`` would pin that request transaction while the agent and rclone are called.
    This reader deliberately owns and closes a tiny session for each read instead.
    """

    database: Database
    secret_box: SecretBox

    async def get_section(self, section: SettingsSection) -> SectionModel:
        async with self.database.session() as session:
            service = SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=self.secret_box,
            )
            return await service.get_section(section)


def build_container(settings: Settings, *, started_at: datetime) -> Container:
    root_secret = settings.secret_key.get_secret_value()
    secret_box = SecretBox(root_secret)
    database = Database(settings)
    agent = AgentClient(settings)
    proxmox = ProxmoxClient(settings)
    telegram = TelegramClient(settings)
    events = EventBus(queue_size=settings.events_queue_size)
    retention_guard = RetentionGuard()

    notification_worker = NotificationWorker(
        database=database,
        telegram=telegram,
        events=events,
        secret_box=secret_box,
        settings=settings,
    )
    # One gateway, shared by every emitter: the factory binds a sink to whichever transaction
    # is recording the state change, and the doorbell wakes delivery once it commits.
    notifications = NotificationGateway(
        factory=lambda session: NotificationService(
            notifications=SqlAlchemyNotificationRepository(session),
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=secret_box
            ),
            max_attempts=settings.notification_max_attempts,
            suppress_window_seconds=settings.notification_suppress_window_seconds,
        ),
        doorbell=notification_worker,
    )
    log_writer = LogWriter(database=database, settings=settings, secret_box=secret_box)

    retention_worker = RetentionWorker(
        database=database,
        agent=agent,
        secret_box=secret_box,
        guard=retention_guard,
        notifications=notifications,
    )
    storage_sampler = StorageSampler(
        database=database,
        agent=agent,
        events=events,
        settings=settings,
        secret_box=secret_box,
        notifications=notifications,
    )

    runner = BackupRunner(
        database=database,
        agent=agent,
        events=events,
        secret_box=secret_box,
        settings=settings,
        retention=retention_worker,
        retention_guard=retention_guard,
        notifications=notifications,
    )
    sync_worker = SyncWorker(
        database=database,
        agent=agent,
        events=events,
        secret_box=secret_box,
        settings=settings,
        retention=retention_worker,
        retention_guard=retention_guard,
        notifications=notifications,
    )
    restore_worker = RestoreWorker(
        database=database,
        agent=agent,
        events=events,
        secret_box=secret_box,
        settings=settings,
        retention_guard=retention_guard,
        notifications=notifications,
    )
    scheduler = SchedulerWorker(
        database=database,
        runner=runner,
        events=events,
        settings=settings,
        secret_box=secret_box,
        inventory_factory=lambda session: InventoryService(
            guests=SqlAlchemyGuestRepository(session),
            proxmox=proxmox,
            settings_service=SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=secret_box
            ),
            events=events,
        ),
    )

    return Container(
        settings=settings,
        database=database,
        passwords=PasswordService(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost,
            parallelism=settings.argon2_parallelism,
        ),
        tokens=TokenService(
            signing_key=derive_jwt_key(root_secret),
            algorithm=settings.jwt_algorithm,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            access_ttl_seconds=settings.access_token_ttl_seconds,
            refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
        ),
        secret_box=secret_box,
        agent=agent,
        proxmox=proxmox,
        telegram=telegram,
        events=events,
        login_limiter=SlidingWindowLimiter(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_attempt_window_seconds,
        ),
        started_at=started_at,
        runner=runner,
        scheduler=scheduler,
        sync_worker=sync_worker,
        restore_worker=restore_worker,
        retention_worker=retention_worker,
        storage_sampler=storage_sampler,
        notification_worker=notification_worker,
        log_writer=log_writer,
        retention_guard=retention_guard,
    )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    session = container.database.session_factory()
    try:
        yield session
        await session.commit()
    except AppError as exc:
        # Expected domain errors keep their side effects (audit rows, lockout counters).
        if exc.status_code < 500:
            await session.commit()
        else:
            await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ---- repositories ------------------------------------------------------------


def get_user_repository(session: SessionDep) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session)


def get_refresh_token_repository(session: SessionDep) -> SqlAlchemyRefreshTokenRepository:
    return SqlAlchemyRefreshTokenRepository(session)


def get_settings_repository(session: SessionDep) -> SqlAlchemySettingsRepository:
    return SqlAlchemySettingsRepository(session)


def get_audit_repository(session: SessionDep) -> SqlAlchemyAuditRepository:
    return SqlAlchemyAuditRepository(session)


def get_guest_repository(session: SessionDep) -> SqlAlchemyGuestRepository:
    return SqlAlchemyGuestRepository(session)


def get_backup_job_repository(session: SessionDep) -> SqlAlchemyBackupJobRepository:
    return SqlAlchemyBackupJobRepository(session)


def get_backup_run_repository(session: SessionDep) -> SqlAlchemyBackupRunRepository:
    return SqlAlchemyBackupRunRepository(session)


def get_backup_history_repository(session: SessionDep) -> SqlAlchemyBackupHistoryRepository:
    return SqlAlchemyBackupHistoryRepository(session)


def get_sync_task_repository(session: SessionDep) -> SqlAlchemySyncTaskRepository:
    return SqlAlchemySyncTaskRepository(session)


def get_retention_repository(session: SessionDep) -> SqlAlchemyRetentionRepository:
    return SqlAlchemyRetentionRepository(session)


def get_restore_repository(session: SessionDep) -> SqlAlchemyRestoreRepository:
    return SqlAlchemyRestoreRepository(session)


def get_storage_snapshot_repository(session: SessionDep) -> SqlAlchemyStorageSnapshotRepository:
    return SqlAlchemyStorageSnapshotRepository(session)


def get_notification_repository(session: SessionDep) -> SqlAlchemyNotificationRepository:
    return SqlAlchemyNotificationRepository(session)


def get_log_repository(session: SessionDep) -> SqlAlchemyLogRepository:
    return SqlAlchemyLogRepository(session)


# ---- services ----------------------------------------------------------------


def get_auth_service(
    container: ContainerDep,
    users: Annotated[SqlAlchemyUserRepository, Depends(get_user_repository)],
    refresh_tokens: Annotated[
        SqlAlchemyRefreshTokenRepository, Depends(get_refresh_token_repository)
    ],
    audit: Annotated[SqlAlchemyAuditRepository, Depends(get_audit_repository)],
) -> AuthService:
    return AuthService(
        settings=container.settings,
        users=users,
        refresh_tokens=refresh_tokens,
        audit=audit,
        passwords=container.passwords,
        tokens=container.tokens,
        limiter=container.login_limiter,
    )


def get_settings_service(
    container: ContainerDep,
    repository: Annotated[SqlAlchemySettingsRepository, Depends(get_settings_repository)],
) -> SettingsService:
    return SettingsService(repository=repository, secret_box=container.secret_box)


def get_health_service(container: ContainerDep) -> HealthService:
    return HealthService(
        settings=container.settings,
        database=container.database,
        agent=container.agent,
        started_at=container.started_at,
        proxmox=container.proxmox,
        runner=container.runner,
        scheduler=container.scheduler,
        sync_worker=container.sync_worker,
        restore_worker=container.restore_worker,
        retention_worker=container.retention_worker,
        sampler=container.storage_sampler,
        notification_worker=container.notification_worker,
        log_writer=container.log_writer,
    )


def get_inventory_service(
    container: ContainerDep,
    guests: Annotated[SqlAlchemyGuestRepository, Depends(get_guest_repository)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> InventoryService:
    return InventoryService(
        guests=guests,
        proxmox=container.proxmox,
        settings_service=settings_service,
        events=container.events,
    )


def get_backup_service(
    guests: Annotated[SqlAlchemyGuestRepository, Depends(get_guest_repository)],
    runs: Annotated[SqlAlchemyBackupRunRepository, Depends(get_backup_run_repository)],
    history: Annotated[SqlAlchemyBackupHistoryRepository, Depends(get_backup_history_repository)],
    restores: Annotated[SqlAlchemyRestoreRepository, Depends(get_restore_repository)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    container: ContainerDep,
) -> BackupService:
    return BackupService(
        guests=guests,
        runs=runs,
        history=history,
        settings_service=settings_service,
        agent=container.agent,
        restores=restores,
    )


def get_sync_service(
    container: ContainerDep,
    tasks: Annotated[SqlAlchemySyncTaskRepository, Depends(get_sync_task_repository)],
    history: Annotated[SqlAlchemyBackupHistoryRepository, Depends(get_backup_history_repository)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> SyncService:
    return SyncService(
        tasks=tasks,
        history=history,
        settings_service=settings_service,
        agent=container.agent,
    )


def get_retention_service(
    container: ContainerDep,
    repository: Annotated[SqlAlchemyRetentionRepository, Depends(get_retention_repository)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> RetentionService:
    return RetentionService(
        repository=repository,
        settings_service=settings_service,
        agent=container.agent,
    )


def get_storage_service(
    container: ContainerDep,
    snapshots: Annotated[
        SqlAlchemyStorageSnapshotRepository,
        Depends(get_storage_snapshot_repository),
    ],
) -> StorageService:
    return StorageService(
        snapshots=snapshots,
        settings_service=DetachedSettingsReader(
            database=container.database,
            secret_box=container.secret_box,
        ),
        agent=container.agent,
    )


def get_restore_service(
    container: ContainerDep,
    restores: Annotated[SqlAlchemyRestoreRepository, Depends(get_restore_repository)],
    history: Annotated[SqlAlchemyBackupHistoryRepository, Depends(get_backup_history_repository)],
    guests: Annotated[SqlAlchemyGuestRepository, Depends(get_guest_repository)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> RestoreService:
    return RestoreService(
        restores=restores,
        history=history,
        guests=guests,
        settings_service=settings_service,
        agent=container.agent,
        confirmation_ttl_seconds=container.settings.restore_confirmation_ttl_seconds,
    )


def get_schedule_service(
    jobs: Annotated[SqlAlchemyBackupJobRepository, Depends(get_backup_job_repository)],
    backups: Annotated[BackupService, Depends(get_backup_service)],
) -> ScheduleService:
    return ScheduleService(jobs=jobs, backups=backups)


def get_notification_service(
    container: ContainerDep,
    notifications: Annotated[
        SqlAlchemyNotificationRepository, Depends(get_notification_repository)
    ],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> NotificationService:
    return NotificationService(
        notifications=notifications,
        settings_service=settings_service,
        telegram=container.telegram,
        max_attempts=container.settings.notification_max_attempts,
        suppress_window_seconds=container.settings.notification_suppress_window_seconds,
    )


def get_log_service(
    logs: Annotated[SqlAlchemyLogRepository, Depends(get_log_repository)],
    audit: Annotated[SqlAlchemyAuditRepository, Depends(get_audit_repository)],
) -> LogService:
    return LogService(logs=logs, audit=audit)


def get_dashboard_service(
    guests: Annotated[SqlAlchemyGuestRepository, Depends(get_guest_repository)],
    runs: Annotated[SqlAlchemyBackupRunRepository, Depends(get_backup_run_repository)],
    history: Annotated[SqlAlchemyBackupHistoryRepository, Depends(get_backup_history_repository)],
    jobs: Annotated[SqlAlchemyBackupJobRepository, Depends(get_backup_job_repository)],
    snapshots: Annotated[
        SqlAlchemyStorageSnapshotRepository, Depends(get_storage_snapshot_repository)
    ],
    restores: Annotated[SqlAlchemyRestoreRepository, Depends(get_restore_repository)],
    sync_tasks: Annotated[SqlAlchemySyncTaskRepository, Depends(get_sync_task_repository)],
    health_service: Annotated[HealthService, Depends(get_health_service)],
) -> DashboardService:
    return DashboardService(
        guests=guests,
        runs=runs,
        history=history,
        jobs=jobs,
        snapshots=snapshots,
        restores=restores,
        sync_tasks=sync_tasks,
        health_service=health_service,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
AuditRepositoryDep = Annotated[SqlAlchemyAuditRepository, Depends(get_audit_repository)]
InventoryServiceDep = Annotated[InventoryService, Depends(get_inventory_service)]
BackupServiceDep = Annotated[BackupService, Depends(get_backup_service)]
ScheduleServiceDep = Annotated[ScheduleService, Depends(get_schedule_service)]
SyncServiceDep = Annotated[SyncService, Depends(get_sync_service)]
RestoreServiceDep = Annotated[RestoreService, Depends(get_restore_service)]
RetentionServiceDep = Annotated[RetentionService, Depends(get_retention_service)]
StorageServiceDep = Annotated[StorageService, Depends(get_storage_service)]
NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]
LogServiceDep = Annotated[LogService, Depends(get_log_service)]
DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]
GuestRepositoryDep = Annotated[SqlAlchemyGuestRepository, Depends(get_guest_repository)]
BackupHistoryRepositoryDep = Annotated[
    SqlAlchemyBackupHistoryRepository, Depends(get_backup_history_repository)
]
BackupRunRepositoryDep = Annotated[
    SqlAlchemyBackupRunRepository, Depends(get_backup_run_repository)
]


# ---- request context ---------------------------------------------------------


def get_request_context(request: Request) -> RequestContext:
    return RequestContext(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


RequestContextDep = Annotated[RequestContext, Depends(get_request_context)]


def verify_csrf(request: Request) -> None:
    """Double-submit check for cookie-authenticated mutations.

    Bearer-authenticated endpoints do not need this: an `Authorization` header is not sent
    automatically by a browser, so it cannot be forged cross-site. Only the refresh cookie is
    ambient, so only the endpoints that consume it are guarded here.
    """
    if not csrf_tokens_match(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise CsrfFailed("CSRF token missing or does not match. Reload the page and try again.")


# ---- authentication ----------------------------------------------------------


async def get_current_user(
    request: Request,
    container: ContainerDep,
    users: Annotated[SqlAlchemyUserRepository, Depends(get_user_repository)],
) -> User:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationFailed("Missing bearer token")

    claims = container.tokens.decode_access_token(token)
    user = await users.get(claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationFailed("This account is no longer available")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_role(minimum: UserRole) -> Callable[[User], User]:
    """Role gate. Also blocks every route except the password change while a password reset
    is outstanding, so a forced rotation cannot be sidestepped by going straight to the API."""

    def dependency(user: CurrentUserDep) -> User:
        if user.must_change_password:
            raise PermissionDenied(
                "You must change your password before using the rest of the application."
            )
        if not user.role_enum.can_act_as(minimum):
            raise PermissionDenied(
                f"This action requires the '{minimum.value}' role or higher; "
                f"your role is '{user.role}'."
            )
        return user

    return dependency


RequireViewer = Annotated[User, Depends(require_role(UserRole.VIEWER))]
RequireOperator = Annotated[User, Depends(require_role(UserRole.OPERATOR))]
RequireAdmin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
