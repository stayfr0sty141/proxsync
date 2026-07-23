"""Retention preview HTTP contract and side-effect boundary."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.backup import BackupHistory, RetentionEvent

from .conftest import ApiClient, StubAgentTransport
from .test_retention_service import add_rows, backup, configure


async def test_preview_is_read_only(
    authenticated_client: ApiClient,
    session_factory: async_sessionmaker[AsyncSession],
    agent_transport: StubAgentTransport,
) -> None:
    await configure(session_factory, keep_local=2, keep_remote=0)
    rows = [backup(31), backup(32), backup(33)]
    await add_rows(session_factory, rows)
    agent_transport.requests.clear()

    response = authenticated_client.post(
        "/api/v1/retention/preview",
        {"keep_local": 1, "keep_remote": 0, "backup_id": rows[-1].id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["policy"]["dry_run"] is True
    assert body["policy"]["keep_local"] == 1
    assert body["summary"]["local_delete_count"] == 2
    assert agent_transport.requests == []

    async with session_factory() as session:
        event_count = await session.scalar(select(func.count(RetentionEvent.id)))
        stored = list(
            (
                await session.execute(
                    select(BackupHistory).where(BackupHistory.id.in_([row.id for row in rows]))
                )
            ).scalars()
        )
    assert event_count == 0
    assert all(row.local_deleted_at is None for row in stored)
