"""Request signing: authenticity, freshness and replay protection."""

from __future__ import annotations

import ipaddress
import time

import pytest

from app.core.errors import AuthenticationFailed, ClientNotAllowed
from app.core.security import (
    NonceCache,
    RequestAuthenticator,
    assert_client_allowed,
    sign_request,
)

SECRET = "correct-horse-battery-staple"  # noqa: S105 - test fixture
KEY_ID = "dashboard"
PATH = "/backup/start"
BODY = b'{"vmid": 101}'


def build_authenticator(window: int = 60) -> RequestAuthenticator:
    return RequestAuthenticator(
        key_id=KEY_ID,
        secret=SECRET,
        window_seconds=window,
        nonce_cache=NonceCache(ttl_seconds=window * 2, max_size=128),
    )


def headers_for(
    *,
    timestamp: str | None = None,
    nonce: str = "nonce-000000001",
    body: bytes = BODY,
    method: str = "POST",
    path: str = PATH,
    secret: str = SECRET,
    key_id: str = KEY_ID,
) -> dict[str, str]:
    stamp = timestamp or str(int(time.time()))
    return {
        "X-ProxSync-Key": key_id,
        "X-ProxSync-Timestamp": stamp,
        "X-ProxSync-Nonce": nonce,
        "X-ProxSync-Signature": sign_request(
            secret=secret, method=method, path=path, timestamp=stamp, nonce=nonce, body=body
        ),
    }


def test_valid_signature_is_accepted() -> None:
    auth = build_authenticator()
    auth.verify(method="POST", path=PATH, headers=headers_for(), body=BODY)


def test_tampered_body_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for()
    with pytest.raises(AuthenticationFailed, match="Invalid request signature"):
        auth.verify(method="POST", path=PATH, headers=headers, body=b'{"vmid": 999}')


def test_tampered_path_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for()
    with pytest.raises(AuthenticationFailed, match="Invalid request signature"):
        auth.verify(method="POST", path="/restore/vm", headers=headers, body=BODY)


def test_tampered_method_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for()
    with pytest.raises(AuthenticationFailed, match="Invalid request signature"):
        auth.verify(method="DELETE", path=PATH, headers=headers, body=BODY)


def test_wrong_secret_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for(secret="wrong-secret")
    with pytest.raises(AuthenticationFailed, match="Invalid request signature"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_unknown_key_id_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for(key_id="attacker")
    with pytest.raises(AuthenticationFailed, match="Unknown key id"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


@pytest.mark.parametrize(
    "missing",
    ["X-ProxSync-Key", "X-ProxSync-Timestamp", "X-ProxSync-Nonce", "X-ProxSync-Signature"],
)
def test_missing_headers_are_rejected(missing: str) -> None:
    auth = build_authenticator()
    headers = headers_for()
    del headers[missing]
    with pytest.raises(AuthenticationFailed, match="Missing request signature headers"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_stale_timestamp_is_rejected() -> None:
    auth = build_authenticator(window=60)
    stale = str(int(time.time()) - 3600)
    headers = headers_for(timestamp=stale)
    with pytest.raises(AuthenticationFailed, match="outside the permitted window"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_future_timestamp_is_rejected() -> None:
    auth = build_authenticator(window=60)
    future = str(int(time.time()) + 3600)
    headers = headers_for(timestamp=future)
    with pytest.raises(AuthenticationFailed, match="outside the permitted window"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_malformed_timestamp_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for()
    headers["X-ProxSync-Timestamp"] = "not-a-number"
    with pytest.raises(AuthenticationFailed, match="Malformed timestamp"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_replayed_nonce_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for(nonce="replay-me-12345")
    auth.verify(method="POST", path=PATH, headers=headers, body=BODY)
    with pytest.raises(AuthenticationFailed, match="Replayed nonce"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_short_nonce_is_rejected() -> None:
    auth = build_authenticator()
    headers = headers_for(nonce="short")
    with pytest.raises(AuthenticationFailed, match="Malformed nonce"):
        auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_header_lookup_is_case_insensitive() -> None:
    auth = build_authenticator()
    headers = {name.lower(): value for name, value in headers_for().items()}
    auth.verify(method="POST", path=PATH, headers=headers, body=BODY)


def test_empty_secret_refuses_construction() -> None:
    nonce_cache = NonceCache(ttl_seconds=120, max_size=8)
    with pytest.raises(ValueError, match="refusing to start unauthenticated"):
        RequestAuthenticator(
            key_id=KEY_ID,
            secret="",
            window_seconds=60,
            nonce_cache=nonce_cache,
        )


def test_nonce_cache_evicts_expired_entries() -> None:
    cache = NonceCache(ttl_seconds=10, max_size=100)
    assert cache.claim("aaaaaaaa", now=1000.0)
    assert not cache.claim("aaaaaaaa", now=1005.0)
    assert cache.claim("aaaaaaaa", now=1020.0)  # outside the TTL, so reusable


def test_nonce_cache_respects_max_size() -> None:
    cache = NonceCache(ttl_seconds=1000, max_size=4)
    for index in range(10):
        cache.claim(f"nonce-{index:04d}", now=1000.0)
    assert len(cache) == 4


class TestClientFilter:
    NETWORKS = (ipaddress.ip_network("10.0.0.0/24"), ipaddress.ip_network("127.0.0.1/32"))

    def test_allows_listed_address(self) -> None:
        assert_client_allowed("10.0.0.15", self.NETWORKS)
        assert_client_allowed("127.0.0.1", self.NETWORKS)

    def test_rejects_unlisted_address(self) -> None:
        with pytest.raises(ClientNotAllowed):
            assert_client_allowed("192.168.1.10", self.NETWORKS)

    def test_rejects_unknown_address(self) -> None:
        with pytest.raises(ClientNotAllowed):
            assert_client_allowed(None, self.NETWORKS)

    def test_rejects_malformed_address(self) -> None:
        with pytest.raises(ClientNotAllowed):
            assert_client_allowed("not-an-ip", self.NETWORKS)

    def test_empty_allow_list_permits_everything(self) -> None:
        assert_client_allowed("203.0.113.5", ())
