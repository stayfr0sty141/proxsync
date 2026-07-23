"""Bounded hand-off from structlog to the `logs` table.

A log call must never block and never fail. Writing to the database from inside a processor
would do both: processors are synchronous, the database is async, and the call site is often
already inside a transaction — so the write would either deadlock on SQLite's single writer or
recurse through SQLAlchemy's own logging.

So the processor does one thing: append to a bounded in-memory deque and return. `LogWriter`
drains it. When the deque is full the **oldest** entry is discarded, exactly as the event bus
drops the oldest event: losing the start of an incident is bad, but wedging a backup because
logging is behind is worse. The drop count is surfaced in `/health/detail` so the loss is
never silent.

Only structlog calls are captured. Standard-library loggers (SQLAlchemy, uvicorn, httpx) do
not pass through these processors, which is what stops the writer's own queries from
generating the log lines that describe them.
"""

from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.schemas.enums import LogCategory, LogLevel

MAX_MESSAGE_LENGTH = 2000
MAX_CONTEXT_VALUE_LENGTH = 1000
MAX_CONTEXT_KEYS = 40

_LEVEL_ORDER = {
    LogLevel.DEBUG: 10,
    LogLevel.INFO: 20,
    LogLevel.WARNING: 30,
    LogLevel.ERROR: 40,
    LogLevel.CRITICAL: 50,
}

_ALIASES = {"warn": LogLevel.WARNING, "fatal": LogLevel.CRITICAL, "exception": LogLevel.ERROR}

# structlog bookkeeping the renderer has already consumed, plus the correlation id, which has
# a column of its own and appears on every single line.
#
# The *other* promoted keys stay in `context` on purpose. They are foreign keys, and a log line
# can name a row that is not committed yet or has since been deleted; when the writer has to
# null the column to get the batch in, the id must still be readable somewhere.
_DROPPED = frozenset(
    {"event", "level", "timestamp", "logger", "logger_name", "category", "correlation_id"}
)

# Prefixes are how a category is inferred when a call site does not name one. Longest match
# wins, so `restore_download_started` is a restore rather than an unknown.
_CATEGORY_PREFIXES: tuple[tuple[str, LogCategory], ...] = (
    ("backup_run", LogCategory.BACKUP),
    ("backup", LogCategory.BACKUP),
    ("restore", LogCategory.RESTORE),
    ("sync", LogCategory.UPLOAD),
    ("upload", LogCategory.UPLOAD),
    ("retention", LogCategory.RETENTION),
    ("schedule", LogCategory.SCHEDULER),
    ("scheduler", LogCategory.SCHEDULER),
    ("job", LogCategory.SCHEDULER),
    ("notification", LogCategory.NOTIFY),
    ("telegram", LogCategory.NOTIFY),
    ("login", LogCategory.AUTH),
    ("auth", LogCategory.AUTH),
    ("token", LogCategory.AUTH),
    ("password", LogCategory.AUTH),
    ("account", LogCategory.AUTH),
    ("user", LogCategory.AUTH),
    ("agent", LogCategory.AGENT),
    ("proxmox", LogCategory.AGENT),
    ("request", LogCategory.API),
    ("inventory", LogCategory.API),
)

_suppressed: ContextVar[bool] = ContextVar("log_sink_suppressed", default=False)


@dataclass(frozen=True, slots=True)
class CapturedLog:
    """One structlog event, flattened into the shape the `logs` table stores."""

    ts: datetime
    level: LogLevel
    category: LogCategory
    message: str
    context: dict[str, Any] | None
    correlation_id: str | None
    user_id: int | None
    backup_id: int | None
    restore_id: int | None
    sync_task_id: int | None


class LogSink:
    def __init__(self, *, capacity: int = 2000, level: LogLevel = LogLevel.INFO) -> None:
        self._entries: deque[CapturedLog] = deque(maxlen=capacity)
        self._level = level
        self._enabled = False
        self._dropped = 0

    # ---- configuration -------------------------------------------------------

    def configure(self, *, capacity: int, level: LogLevel, enabled: bool) -> None:
        if capacity != self._entries.maxlen:
            self._entries = deque(self._entries, maxlen=capacity)
        self._level = level
        self._enabled = enabled

    def disable(self) -> None:
        self._enabled = False

    def reset(self) -> None:
        """Discard everything buffered and the drop count, and stop capturing.

        The sink is process-wide, so it outlives any single application instance. Anything
        that creates more than one — a test suite, a reload — needs a way back to a clean
        state, or a buffer nobody drains keeps counting drops against the next one.
        """
        self._entries.clear()
        self._dropped = 0
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def dropped(self) -> int:
        """Entries discarded because the writer fell behind. Reported by `/health/detail`."""
        return self._dropped

    @property
    def pending(self) -> int:
        return len(self._entries)

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """Stop capturing inside this block.

        Held by the writer while it talks to the database, so a failure to persist logs cannot
        generate the log line that describes it and refill the queue it just drained.
        """
        token = _suppressed.set(True)
        try:
            yield
        finally:
            _suppressed.reset(token)

    # ---- capture -------------------------------------------------------------

    def capture(self, _logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        """structlog processor. Returns `event_dict` unchanged, always."""
        if not self._enabled or _suppressed.get():
            return event_dict
        try:
            entry = self._build(event_dict)
        except Exception:  # noqa: BLE001 - a broken log line must not break the caller
            return event_dict
        if entry is not None:
            if len(self._entries) == self._entries.maxlen:
                self._dropped += 1
            self._entries.append(entry)
        return event_dict

    def _build(self, event_dict: dict[str, Any]) -> CapturedLog | None:
        level = _parse_level(event_dict.get("level"))
        if _LEVEL_ORDER[level] < _LEVEL_ORDER[self._level]:
            return None

        message = str(event_dict.get("event", ""))[:MAX_MESSAGE_LENGTH]
        return CapturedLog(
            ts=_parse_timestamp(event_dict.get("timestamp")),
            level=level,
            category=_resolve_category(event_dict, message),
            message=message,
            context=_context(event_dict),
            correlation_id=_as_str(event_dict.get("correlation_id")),
            user_id=_as_int(event_dict.get("user_id")),
            backup_id=_as_int(event_dict.get("backup_id")),
            restore_id=_as_int(event_dict.get("restore_id")),
            sync_task_id=_as_int(event_dict.get("sync_task_id")),
        )

    # ---- drain ---------------------------------------------------------------

    def drain(self, limit: int) -> list[CapturedLog]:
        """Take up to `limit` entries, oldest first."""
        taken: list[CapturedLog] = []
        while self._entries and len(taken) < limit:
            taken.append(self._entries.popleft())
        return taken

    def restore(self, entries: list[CapturedLog]) -> None:
        """Put a failed batch back at the front, so a transient database error costs latency
        rather than the lines themselves."""
        self._entries.extendleft(reversed(entries))


def _parse_level(value: Any) -> LogLevel:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in _ALIASES:
            return _ALIASES[lowered]
        with contextlib.suppress(ValueError):
            return LogLevel(lowered)
    return LogLevel.INFO


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _resolve_category(event_dict: dict[str, Any], message: str) -> LogCategory:
    """An explicit `category=` wins; otherwise the event name decides."""
    declared = event_dict.get("category")
    if isinstance(declared, str):
        with contextlib.suppress(ValueError):
            return LogCategory(declared)
    for prefix, category in _CATEGORY_PREFIXES:
        if message.startswith(prefix):
            return category
    return LogCategory.SYSTEM


def _context(event_dict: dict[str, Any]) -> dict[str, Any] | None:
    context: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key in _DROPPED or len(context) >= MAX_CONTEXT_KEYS:
            continue
        context[key] = _jsonable(value)
    return context or None


def _jsonable(value: Any) -> Any:
    """Coerce to something both JSON and JSONB accept.

    Anything exotic becomes its `repr`, truncated. A log line is diagnostic text; refusing to
    store it because one field held a socket object would be the wrong trade.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_CONTEXT_VALUE_LENGTH]
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in list(value.items())[:20]}
    if isinstance(value, datetime):
        return value.isoformat()
    return repr(value)[:MAX_CONTEXT_VALUE_LENGTH]


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    return value[:64] if isinstance(value, str) and value else None


log_sink = LogSink()
"""Process-wide sink.

A module-level singleton for the same reason `logger` is one: the processor chain is installed
once, at import-adjacent time, long before a container or a database session exists. Capture
is off until `configure_logging` turns it on.
"""
