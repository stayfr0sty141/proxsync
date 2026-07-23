"""Log and audit-trail API shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.enums import AuditAction, AuditResult, LogCategory, LogLevel

ExportFormat = Literal["ndjson", "csv"]


class LogEntryResponse(BaseModel):
    id: int
    ts: datetime
    level: LogLevel
    category: LogCategory
    message: str
    context: dict[str, Any] | None = None
    correlation_id: str | None = None
    user_id: int | None = None
    backup_id: int | None = None
    restore_id: int | None = None
    sync_task_id: int | None = None


class LogListResponse(BaseModel):
    items: list[LogEntryResponse]
    total: int
    persisted: bool
    """False when log persistence is switched off, so an empty list reads as "not recorded"
    rather than "nothing happened"."""
    dropped: int
    """Entries the sink discarded because the writer fell behind. Non-zero means this page is
    incomplete, and saying so is the whole point of counting them."""


class AuditEventResponse(BaseModel):
    id: int
    ts: datetime
    action: AuditAction
    result: AuditResult
    user_id: int | None = None
    username: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    detail: dict[str, Any] | None = None
    correlation_id: str | None = None


class AuditListResponse(BaseModel):
    items: list[AuditEventResponse]
    total: int
