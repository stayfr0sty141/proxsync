"""Circuit breaker.

Without one, an agent that is down turns every dashboard page into a 30-second timeout. With
one, the dashboard learns in a single request and can say "agent offline" immediately, which
is both faster and more truthful.
"""

from __future__ import annotations

import time
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, reset_seconds: int, name: str = "agent") -> None:
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._name = name
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False

    @property
    def name(self) -> str:
        return self._name

    def state(self, *, now: float | None = None) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        current = time.monotonic() if now is None else now
        if current - self._opened_at >= self._reset:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    @property
    def failure_count(self) -> int:
        return self._failures

    def allow(self, *, now: float | None = None) -> bool:
        """Whether a request may proceed. Half-open admits exactly one probe."""
        state = self.state(now=now)
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.OPEN:
            return False
        if self._half_open_in_flight:
            return False
        self._half_open_in_flight = True
        return True

    def retry_after_seconds(self, *, now: float | None = None) -> int:
        if self._opened_at is None:
            return 0
        current = time.monotonic() if now is None else now
        return max(0, int(self._opened_at + self._reset - current))

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self._failures += 1
        self._half_open_in_flight = False
        if self._failures >= self._threshold:
            # Re-stamp on every failure while open, so a persistently dead agent keeps the
            # circuit open rather than being probed every `reset_seconds` from the first fault.
            self._opened_at = current

    def reset(self) -> None:
        self.record_success()
