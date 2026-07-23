"""In-process sliding-window rate limiting.

Sized for a single-container deployment, which is what ProxSync is. If the dashboard is ever
run with multiple workers, this must move to a shared store — the limiter is deliberately
small and behind an interface so that swap is contained.

Login protection is two-layered: this limiter throttles by (IP, username) so an attacker
cannot spray one account from one address, and the user row carries `failed_login_count` /
`locked_until` so the account stays protected even from a rotating set of addresses.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class SlidingWindowLimiter:
    def __init__(self, *, max_attempts: int, window_seconds: int, max_keys: int = 4096) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float]:
        window = self._hits.get(key)
        if window is None:
            window = deque()
            self._hits[key] = window
        cutoff = now - self._window
        while window and window[0] < cutoff:
            window.popleft()
        return window

    def check(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        """Report whether ``key`` may proceed, without consuming an attempt."""
        current = time.monotonic() if now is None else now
        window = self._prune(key, current)
        if len(window) < self._max:
            return RateLimitDecision(True, self._max - len(window), 0)
        retry_after = max(1, int(window[0] + self._window - current))
        return RateLimitDecision(False, 0, retry_after)

    def record(self, key: str, *, now: float | None = None) -> None:
        """Consume one attempt. Called on failure only — a successful login costs nothing."""
        current = time.monotonic() if now is None else now
        window = self._prune(key, current)
        window.append(current)
        self._hits.move_to_end(key)
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def __len__(self) -> int:
        return len(self._hits)


def lockout_duration_seconds(failed_count: int, *, base: int, maximum: int) -> int:
    """Exponential backoff: base, 2×base, 4×base … capped at ``maximum``.

    ``failed_count`` is the number of consecutive failures *including* the one that triggered
    the lockout, so the first lockout is exactly ``base``.
    """
    if failed_count <= 0:
        return 0
    exponent = min(failed_count - 1, 16)  # 2**16 × base already exceeds any sane maximum
    return min(base * (1 << exponent), maximum)
