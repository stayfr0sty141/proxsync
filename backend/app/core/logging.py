"""structlog configuration and correlation-id propagation.

Every request gets a correlation id — taken from `X-Correlation-ID` when the caller supplies
one, generated otherwise — which is echoed in the response, attached to every log line, and
forwarded to the Backup Agent so a single id ties the browser, the dashboard and the host
together.

The same chain optionally feeds `app.core.log_sink`, which buffers events for the writer that
persists them to the `logs` table. It is installed last, immediately before the renderer, so a
captured entry carries the level, timestamp and formatted exception the console line shows.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.log_sink import LogSink, log_sink
from app.schemas.enums import LogLevel

HEADER_CORRELATION_ID = "X-Correlation-ID"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

logger: structlog.stdlib.BoundLogger = structlog.get_logger("proxsync.api")


def set_correlation_id(value: str | None) -> str:
    resolved = value or uuid.uuid4().hex
    _correlation_id.set(resolved)
    return resolved


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def _inject_correlation_id(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    correlation_id = _correlation_id.get()
    if correlation_id:
        event_dict.setdefault("correlation_id", correlation_id)
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    persist: bool = False,
    persist_level: str = "INFO",
    buffer_size: int = 2000,
    sink: LogSink | None = None,
) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level))

    # SQLAlchemy's own loggers are noisy at INFO and duplicate what `database_echo` controls.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    target = sink or log_sink
    target.configure(capacity=buffer_size, level=LogLevel(persist_level.lower()), enabled=persist)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_correlation_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        target.capture,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
