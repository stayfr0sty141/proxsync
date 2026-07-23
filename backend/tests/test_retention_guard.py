"""The retention mutation guard is the process-local linearisation point."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import build_container
from app.core.config import Settings
from app.core.retention_guard import RetentionGuard


async def test_guard_serialises_retention_affecting_operations() -> None:
    guard = RetentionGuard()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with guard.hold():
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with guard.hold():
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)

    assert order == ["first-enter"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_guard_rolls_back_before_releasing_a_failed_transaction() -> None:
    guard = RetentionGuard()
    session = AsyncMock(spec=AsyncSession)

    with pytest.raises(RuntimeError, match="boom"):
        async with guard.transaction(cast(AsyncSession, session)):
            raise RuntimeError("boom")

    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


async def test_composition_root_shares_one_guard_across_every_mutation_lane(
    settings: Settings,
) -> None:
    container = build_container(settings, started_at=datetime.now(UTC))
    try:
        assert container.retention_worker._guard is container.retention_guard  # noqa: SLF001
        assert container.runner._retention_guard is container.retention_guard  # noqa: SLF001
        assert container.sync_worker._retention_guard is container.retention_guard  # noqa: SLF001
    finally:
        await container.agent.aclose()
        await container.proxmox.aclose()
        await container.database.dispose()
