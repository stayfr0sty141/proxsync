"""Non-blocking concurrency slots.

A busy agent must answer immediately with 409 rather than queue: the dashboard owns
scheduling and needs to know the request was not accepted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlotState:
    name: str
    in_use: int
    capacity: int

    @property
    def available(self) -> int:
        return self.capacity - self.in_use


class SlotBusyError(RuntimeError):
    def __init__(self, name: str, capacity: int) -> None:
        super().__init__(f"slot '{name}' is at capacity ({capacity})")
        self.slot = name
        self.capacity = capacity


class SlotManager:
    """Counting slots with try-acquire semantics.

    Single-threaded by contract: acquisition happens on the event loop only, so a plain
    integer counter is sufficient and avoids awaiting inside request validation.
    """

    def __init__(self, capacities: dict[str, int]) -> None:
        self._capacity = dict(capacities)
        self._in_use: dict[str, int] = dict.fromkeys(capacities, 0)
        self._lock = asyncio.Lock()

    def try_acquire(self, name: str) -> None:
        capacity = self._capacity[name]
        if self._in_use[name] >= capacity:
            raise SlotBusyError(name, capacity)
        self._in_use[name] += 1

    def release(self, name: str) -> None:
        if self._in_use[name] > 0:
            self._in_use[name] -= 1

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        self.try_acquire(name)
        try:
            yield
        finally:
            self.release(name)

    def state(self) -> list[SlotState]:
        return [
            SlotState(name=name, in_use=self._in_use[name], capacity=capacity)
            for name, capacity in self._capacity.items()
        ]
