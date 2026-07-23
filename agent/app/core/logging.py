"""structlog configuration and correlation-id propagation.

Logs go to stdout as JSON so journald captures them verbatim; per-task command output goes to
its own file under ``log_dir/tasks`` and is never mixed into the service log.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

logger: structlog.stdlib.BoundLogger = structlog.get_logger("proxsync.agent")


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def _inject_correlation_id(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    correlation_id = _correlation_id.get()
    if correlation_id:
        event_dict.setdefault("correlation_id", correlation_id)
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_correlation_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if json_output:
        processors.extend(
            [
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ]
        )
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
