"""Entry point: ``python -m app`` / ``proxsync-agent``.

TLS is configured here rather than in a reverse proxy: the agent is the privileged component
and terminates its own mutual TLS so no other process ever sees a decrypted request.
"""

from __future__ import annotations

import ssl
import sys

import uvicorn

from app.core.config import get_settings
from app.core.logging import configure_logging, logger


def main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if not settings.hmac_secret.get_secret_value():
        logger.error(
            "startup_refused",
            detail="PROXSYNC_AGENT_HMAC_SECRET is not set. The agent will not start "
            "without request signing configured.",
        )
        return 2

    ssl_options: dict[str, object] = {}
    if settings.tls_certfile and settings.tls_keyfile:
        ssl_options = {
            "ssl_certfile": str(settings.tls_certfile),
            "ssl_keyfile": str(settings.tls_keyfile),
        }
        if settings.tls_client_ca:
            # Mutual TLS: a handshake without a certificate signed by this CA never reaches
            # the application layer.
            ssl_options["ssl_ca_certs"] = str(settings.tls_client_ca)
            ssl_options["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    else:
        logger.warning(
            "tls_disabled",
            detail="No certificate configured; the agent is serving plain HTTP. "
            "Use this only on an isolated host bridge during development.",
        )

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,
        access_log=False,  # structlog records requests; uvicorn's access log would duplicate
        server_header=False,
        date_header=True,
        **ssl_options,  # type: ignore[arg-type]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
