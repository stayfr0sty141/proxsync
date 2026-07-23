"""Request authentication.

Two independent layers guard the agent:

1. **Transport** — uvicorn is started with ``ssl_cert_reqs=CERT_REQUIRED`` and the dashboard's
   CA, so a TLS handshake without a valid client certificate never reaches the application.
   This module does not re-implement that check; it assumes it.
2. **Application** — every request carries an HMAC-SHA256 signature over a canonical string
   that binds the method, path, query, timestamp, nonce and body. A replayed or altered
   request fails here even if the transport layer is somehow bypassed.

Address filtering runs before both, so an unauthorised source never reaches the HMAC path.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from collections import OrderedDict

from app.core.errors import AuthenticationFailed, ClientNotAllowed

HEADER_KEY_ID = "X-ProxSync-Key"
HEADER_TIMESTAMP = "X-ProxSync-Timestamp"
HEADER_NONCE = "X-ProxSync-Nonce"
HEADER_SIGNATURE = "X-ProxSync-Signature"
HEADER_CORRELATION_ID = "X-Correlation-ID"

_MIN_NONCE_LENGTH = 8
_MAX_NONCE_LENGTH = 128


class NonceCache:
    """Bounded, TTL-evicting nonce store. Rejects replays inside the signature window."""

    def __init__(self, *, ttl_seconds: int, max_size: int) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._entries: OrderedDict[str, float] = OrderedDict()

    def _purge(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._entries:
            _, seen_at = next(iter(self._entries.items()))
            if seen_at >= cutoff:
                break
            self._entries.popitem(last=False)

    def claim(self, nonce: str, *, now: float | None = None) -> bool:
        """Record ``nonce``; return False when it was already used inside the window."""
        current = time.time() if now is None else now
        self._purge(current)
        if nonce in self._entries:
            return False
        self._entries[nonce] = current
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._entries)


def build_canonical_string(
    *, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> str:
    """``METHOD|/path?query|timestamp|nonce|sha256(body)`` — the string that gets signed."""
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}|{path}|{timestamp}|{nonce}|{body_hash}"


def sign_request(
    *, secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> str:
    canonical = build_canonical_string(
        method=method, path=path, timestamp=timestamp, nonce=nonce, body=body
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


class RequestAuthenticator:
    def __init__(
        self,
        *,
        key_id: str,
        secret: str,
        window_seconds: int,
        nonce_cache: NonceCache,
    ) -> None:
        if not secret:
            raise ValueError("hmac_secret must be configured; refusing to start unauthenticated")
        self._key_id = key_id
        self._secret = secret
        self._window = window_seconds
        self._nonces = nonce_cache

    def verify(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        now: float | None = None,
    ) -> None:
        """Raise :class:`AuthenticationFailed` unless the request is authentic and fresh."""
        lookup = {name.lower(): value for name, value in headers.items()}
        key_id = lookup.get(HEADER_KEY_ID.lower())
        timestamp = lookup.get(HEADER_TIMESTAMP.lower())
        nonce = lookup.get(HEADER_NONCE.lower())
        signature = lookup.get(HEADER_SIGNATURE.lower())

        if not (key_id and timestamp and nonce and signature):
            raise AuthenticationFailed("Missing request signature headers")

        # Compared in constant time so a wrong key id cannot be distinguished by timing.
        if not hmac.compare_digest(key_id, self._key_id):
            raise AuthenticationFailed("Unknown key id")

        if not _MIN_NONCE_LENGTH <= len(nonce) <= _MAX_NONCE_LENGTH:
            raise AuthenticationFailed("Malformed nonce")

        try:
            request_time = float(timestamp)
        except ValueError:
            raise AuthenticationFailed("Malformed timestamp") from None

        current = time.time() if now is None else now
        skew = abs(current - request_time)
        if skew > self._window:
            raise AuthenticationFailed(
                f"Timestamp outside the permitted window ({skew:.0f}s of {self._window}s). "
                "Check clock synchronisation between the dashboard and this host."
            )

        expected = sign_request(
            secret=self._secret,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        if not hmac.compare_digest(expected, signature):
            raise AuthenticationFailed("Invalid request signature")

        # Claimed only after the signature verifies, so an attacker cannot burn nonces.
        if not self._nonces.claim(nonce, now=current):
            raise AuthenticationFailed("Replayed nonce")


def assert_client_allowed(
    client_host: str | None,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> None:
    if not networks:
        return
    if client_host is None:
        raise ClientNotAllowed("Client address could not be determined")
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        raise ClientNotAllowed(f"Malformed client address: {client_host}") from None
    if not any(address in network for network in networks):
        raise ClientNotAllowed(f"Address {client_host} is not permitted to reach this agent")
