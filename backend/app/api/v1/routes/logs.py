"""Log search, export, and the audit trail."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import ContainerDep, LogServiceDep, RequireAdmin, RequireViewer
from app.core.log_sink import log_sink
from app.repositories.log_repository import SqlAlchemyLogRepository
from app.schemas.enums import AuditAction, AuditResult, LogCategory, LogLevel
from app.schemas.logs import AuditListResponse, ExportFormat, LogListResponse
from app.services.log_service import encode_csv, encode_ndjson

router = APIRouter(tags=["logs"])

MAX_PAGE_SIZE = 500

_MEDIA_TYPES: dict[ExportFormat, str] = {
    "ndjson": "application/x-ndjson",
    "csv": "text/csv",
}


@router.get("/logs", summary="Application log search")
async def list_logs(
    service: LogServiceDep,
    container: ContainerDep,
    user: RequireViewer,
    category: LogCategory | None = None,
    level: LogLevel | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    search: str | None = None,
    correlation_id: str | None = None,
    backup_id: int | None = None,
    restore_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogListResponse:
    del user
    return await service.search(
        category=category,
        level=level,
        since=date_from,
        until=date_to,
        query=search,
        correlation_id=correlation_id,
        backup_id=backup_id,
        restore_id=restore_id,
        limit=limit,
        offset=offset,
        persisted=container.settings.log_persist_enabled,
        dropped=log_sink.dropped,
    )


@router.get("/logs/export", summary="Stream the filtered log set")
async def export_logs(
    container: ContainerDep,
    user: RequireAdmin,
    export_format: Annotated[ExportFormat, Query(alias="format")] = "ndjson",
    category: LogCategory | None = None,
    level: LogLevel | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    search: str | None = None,
    correlation_id: str | None = None,
) -> StreamingResponse:
    """Stream the whole filtered set, newest first.

    This route deliberately owns its database session instead of taking the request-scoped
    one. A `StreamingResponse` body runs *after* the handler returns, by which point the
    dependency has closed its session — the export would read from a dead connection.
    """
    del user
    filters: dict[str, Any] = {
        "category": category,
        "level": level,
        "since": date_from,
        "until": date_to,
        "query": search,
        "correlation_id": correlation_id,
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 - local filename, not data
    filename = f"proxsync-logs-{stamp}.{export_format}"

    async def body() -> AsyncIterator[bytes]:
        first = True
        async with container.database.session() as session:
            async for chunk in SqlAlchemyLogRepository(session).iterate(**filters):
                if export_format == "csv":
                    yield encode_csv(chunk, header=first).encode()
                else:
                    yield encode_ndjson(chunk).encode()
                first = False
        if first and export_format == "csv":
            # An empty result still gets its header row, so the file opens as a table rather
            # than as nothing at all.
            yield encode_csv([], header=True).encode()

    return StreamingResponse(
        body(),
        media_type=_MEDIA_TYPES[export_format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit", summary="Security audit trail")
async def list_audit(
    service: LogServiceDep,
    user: RequireAdmin,
    action: AuditAction | None = None,
    result: AuditResult | None = None,
    user_id: int | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    search: str | None = None,
    correlation_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    del user
    return await service.audit_search(
        action=action,
        result=result,
        user_id=user_id,
        since=date_from,
        until=date_to,
        query=search,
        correlation_id=correlation_id,
        limit=limit,
        offset=offset,
    )
