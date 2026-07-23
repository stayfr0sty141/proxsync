"""Single-process serialization for retention-affecting state transitions.

The dashboard deliberately runs one uvicorn process because its scheduler and workers are
in-process singletons. Within that process, this guard closes the small but dangerous window
between retention's final database revalidation and physical deletion: pinning, verification,
backup/upload completion and policy writes use the same guard through their commit.

If ProxSync ever supports multiple API worker processes, this must become a durable database
claim or an advisory lock. An in-memory lock must not be mistaken for cross-process safety.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


class RetentionGuard:
    """Serialize decisions with every transition that can invalidate them."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        async with self._lock:
            yield

    @asynccontextmanager
    async def transaction(self, session: AsyncSession) -> AsyncIterator[None]:
        """Hold the guard until mutations and their audit/doorbell hooks are durable.

        The ordinary request dependency commits after a handler returns, one event-loop turn
        too late for this invariant. A guarded route commits explicitly here; the dependency's
        subsequent commit is a harmless no-op.
        """

        async with self.hold():
            try:
                yield
                await session.commit()
            except BaseException:
                # Keep the serialization point until failed work is no longer visible even
                # on cancellation. The outer request dependency may roll back again; like its
                # second commit on success, that is intentionally harmless.
                await session.rollback()
                raise
