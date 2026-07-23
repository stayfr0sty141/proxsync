"""Model registry.

Alembic's autogenerate and `Base.metadata.create_all` both need every model imported before
they run, so this module is the single import that guarantees a complete metadata graph.
"""

from __future__ import annotations

from app.db.base import Base
from app.db.models.backup import (
    BackupHistory,
    BackupJob,
    BackupJobTarget,
    BackupRun,
    RestoreHistory,
    RetentionEvent,
    SyncTask,
)
from app.db.models.guest import Guest
from app.db.models.system import (
    AuditEvent,
    LogEntry,
    Notification,
    Setting,
    StorageSnapshot,
)
from app.db.models.user import RefreshToken, User

__all__ = [
    "AuditEvent",
    "BackupHistory",
    "BackupJob",
    "BackupJobTarget",
    "BackupRun",
    "Base",
    "Guest",
    "LogEntry",
    "Notification",
    "RefreshToken",
    "RestoreHistory",
    "RetentionEvent",
    "Setting",
    "StorageSnapshot",
    "SyncTask",
    "User",
]
