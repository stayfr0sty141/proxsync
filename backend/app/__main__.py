"""Entry point: ``python -m app`` / ``proxsync-api``.

TLS termination and static hosting are nginx's job; this process binds to localhost inside
the container.
"""

from __future__ import annotations

import sys

import uvicorn

from app.core.config import get_settings
from app.core.logging import configure_logging, logger


def main() -> int:
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - configuration errors must be readable
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if settings.environment == "production" and not settings.cookie_secure:
        logger.warning(
            "insecure_cookies",
            detail="PROXSYNC_COOKIE_SECURE is false in production; session cookies will be "
            "sent over plain HTTP.",
        )

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,
        access_log=False,  # structlog records requests; uvicorn's access log would duplicate
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
