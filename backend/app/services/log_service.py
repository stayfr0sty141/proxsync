"""Log and audit reads, and the export encoders.

Two consumers with opposite needs: the UI wants a page with a total, and an export wants the
whole filtered set with no total at all. Counting 400 000 rows to tell someone their download
has 400 000 rows would double the work for no information they do not already get by watching
the file grow.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.db.models.system import AuditEvent, LogEntry
from app.repositories.audit_repository import AuditRepository
from app.repositories.log_repository import LogRepository
from app.schemas.enums import AuditAction, AuditResult, LogCategory, LogLevel
from app.schemas.logs import (
    AuditEventResponse,
    AuditListResponse,
    LogEntryResponse,
    LogListResponse,
)

CSV_COLUMNS = (
    "id",
    "ts",
    "level",
    "category",
    "message",
    "correlation_id",
    "user_id",
    "backup_id",
    "restore_id",
    "sync_task_id",
    "context",
)


class LogService:
    def __init__(self, *, logs: LogRepository, audit: AuditRepository) -> None:
        self._logs = logs
        self._audit = audit

    async def search(
        self,
        *,
        category: LogCategory | None = None,
        level: LogLevel | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        backup_id: int | None = None,
        restore_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
        persisted: bool = True,
        dropped: int = 0,
    ) -> LogListResponse:
        filters: dict[str, Any] = {
            "category": category,
            "level": level,
            "since": since,
            "until": until,
            "query": query,
            "correlation_id": correlation_id,
            "backup_id": backup_id,
            "restore_id": restore_id,
        }
        items = await self._logs.search(limit=limit, offset=offset, **filters)
        return LogListResponse(
            items=[log_to_response(item) for item in items],
            total=await self._logs.count(**filters),
            persisted=persisted,
            dropped=dropped,
        )

    async def audit_search(
        self,
        *,
        action: AuditAction | None = None,
        result: AuditResult | None = None,
        user_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AuditListResponse:
        filters: dict[str, Any] = {
            "action": action,
            "result": result,
            "user_id": user_id,
            "since": since,
            "until": until,
            "query": query,
            "correlation_id": correlation_id,
        }
        items = await self._audit.search(limit=limit, offset=offset, **filters)
        return AuditListResponse(
            items=[audit_to_response(item) for item in items],
            total=await self._audit.count(**filters),
        )


def log_to_response(record: LogEntry) -> LogEntryResponse:
    return LogEntryResponse(
        id=record.id,
        ts=record.ts,
        level=LogLevel(record.level),
        category=LogCategory(record.category),
        message=record.message,
        context=record.context,
        correlation_id=record.correlation_id,
        user_id=record.user_id,
        backup_id=record.backup_id,
        restore_id=record.restore_id,
        sync_task_id=record.sync_task_id,
    )


def audit_to_response(record: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=record.id,
        ts=record.ts,
        action=AuditAction(record.action),
        result=AuditResult(record.result),
        user_id=record.user_id,
        username=record.username_snapshot,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        ip_address=record.ip_address,
        user_agent=record.user_agent,
        detail=record.detail,
        correlation_id=record.correlation_id,
    )


def encode_ndjson(rows: Sequence[LogEntry]) -> str:
    """One JSON object per line — streamable, and readable by `jq` without loading the file."""
    return "".join(
        json.dumps(log_to_response(row).model_dump(mode="json"), separators=(",", ":")) + "\n"
        for row in rows
    )


def encode_csv(rows: Sequence[LogEntry], *, header: bool = False) -> str:
    """CSV for spreadsheets. `context` is serialised as a JSON string in one column.

    `csv.writer` rather than string joining: a vzdump error message containing a comma, a
    quote or a newline is ordinary, and hand-rolled CSV corrupts every row after it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if header:
        writer.writerow(CSV_COLUMNS)
    for row in rows:
        payload = log_to_response(row).model_dump(mode="json")
        payload["context"] = (
            json.dumps(payload["context"], separators=(",", ":")) if payload["context"] else ""
        )
        writer.writerow(
            [payload[column] if payload[column] is not None else "" for column in CSV_COLUMNS]
        )
    return buffer.getvalue()
