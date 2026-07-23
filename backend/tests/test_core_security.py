"""Password hashing, tokens, CSRF, rate limiting, key derivation and the circuit breaker."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.clients.circuit_breaker import CircuitBreaker, CircuitState
from app.core.crypto import SecretBox, derive_jwt_key, derive_key
from app.core.errors import AuthenticationFailed, ConfigurationError
from app.core.rate_limit import SlidingWindowLimiter, lockout_duration_seconds
from app.core.security import (
    PasswordService,
    TokenService,
    csrf_tokens_match,
    generate_csrf_token,
    hash_token,
    new_family_id,
)

ROOT_SECRET = "0123456789abcdef0123456789abcdef"


def build_tokens(*, access_ttl: int = 900) -> TokenService:
    return TokenService(
        signing_key=derive_jwt_key(ROOT_SECRET),
        algorithm="HS256",
        issuer="proxsync",
        audience="proxsync-dashboard",
        access_ttl_seconds=access_ttl,
        refresh_ttl_seconds=604800,
    )


class TestPasswordService:
    @pytest.fixture
    def passwords(self) -> PasswordService:
        return PasswordService(time_cost=1, memory_cost=8, parallelism=1)

    def test_hash_and_verify(self, passwords: PasswordService) -> None:
        digest = passwords.hash("correct horse battery staple")
        assert passwords.verify(digest, "correct horse battery staple")

    def test_wrong_password_is_rejected(self, passwords: PasswordService) -> None:
        digest = passwords.hash("right")
        assert not passwords.verify(digest, "wrong")

    def test_hashes_are_salted(self, passwords: PasswordService) -> None:
        assert passwords.hash("same") != passwords.hash("same")

    def test_hash_is_argon2id(self, passwords: PasswordService) -> None:
        assert passwords.hash("x").startswith("$argon2id$")

    def test_malformed_hash_does_not_raise(self, passwords: PasswordService) -> None:
        assert not passwords.verify("not-a-hash", "anything")

    def test_needs_rehash_when_parameters_increase(self, passwords: PasswordService) -> None:
        weak = passwords.hash("password")
        stronger = PasswordService(time_cost=3, memory_cost=64, parallelism=1)
        assert stronger.needs_rehash(weak)
        assert not passwords.needs_rehash(weak)

    def test_dummy_verify_is_safe_to_call(self, passwords: PasswordService) -> None:
        passwords.dummy_verify()  # must not raise; it exists to equalise timing


class TestAccessTokens:
    def test_round_trip(self) -> None:
        tokens = build_tokens()
        token, claims = tokens.create_access_token(user_id=7, username="ada", role="admin")
        decoded = tokens.decode_access_token(token)

        assert decoded.user_id == 7
        assert decoded.username == "ada"
        assert decoded.role == "admin"
        assert decoded.jti == claims.jti

    def test_expired_token_is_rejected(self) -> None:
        tokens = build_tokens(access_ttl=1)
        past = datetime.now(UTC) - timedelta(hours=1)
        token, _ = tokens.create_access_token(user_id=1, username="ada", role="viewer", now=past)
        with pytest.raises(AuthenticationFailed, match="expired"):
            tokens.decode_access_token(token)

    def test_tampered_token_is_rejected(self) -> None:
        tokens = build_tokens()
        token, _ = tokens.create_access_token(user_id=1, username="ada", role="viewer")
        head, payload, signature = token.split(".")
        with pytest.raises(AuthenticationFailed, match="invalid"):
            tokens.decode_access_token(f"{head}.{payload}x.{signature}")

    def test_token_signed_with_another_key_is_rejected(self) -> None:
        other = TokenService(
            signing_key=derive_jwt_key("f" * 32),
            algorithm="HS256",
            issuer="proxsync",
            audience="proxsync-dashboard",
            access_ttl_seconds=900,
            refresh_ttl_seconds=1,
        )
        token, _ = other.create_access_token(user_id=1, username="mallory", role="admin")
        with pytest.raises(AuthenticationFailed):
            build_tokens().decode_access_token(token)

    def test_wrong_audience_is_rejected(self) -> None:
        tokens = build_tokens()
        payload = {
            "sub": "1",
            "jti": "x",
            "typ": "access",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "iss": "proxsync",
            "aud": "somewhere-else",
        }
        forged = jwt.encode(payload, derive_jwt_key(ROOT_SECRET), algorithm="HS256")
        with pytest.raises(AuthenticationFailed):
            tokens.decode_access_token(forged)

    def test_refresh_token_is_not_accepted_as_access(self) -> None:
        tokens = build_tokens()
        payload = {
            "sub": "1",
            "jti": "x",
            "typ": "refresh",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "iss": "proxsync",
            "aud": "proxsync-dashboard",
        }
        forged = jwt.encode(payload, derive_jwt_key(ROOT_SECRET), algorithm="HS256")
        with pytest.raises(AuthenticationFailed, match="not an access token"):
            tokens.decode_access_token(forged)

    def test_algorithm_none_is_rejected(self) -> None:
        """The classic JWT downgrade: an unsigned token must never be trusted."""
        tokens = build_tokens()
        forged = jwt.encode(
            {"sub": "1", "jti": "x", "typ": "access", "iat": 0, "exp": 9999999999},
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthenticationFailed):
            tokens.decode_access_token(forged)


class TestRefreshTokens:
    def test_generated_tokens_are_unique_and_hashed(self) -> None:
        tokens = build_tokens()
        first = tokens.generate_refresh_token()
        second = tokens.generate_refresh_token()

        assert first.raw != second.raw
        assert first.digest == hash_token(first.raw)
        assert len(first.digest) == 64
        assert first.raw not in first.digest  # the raw token is never recoverable from the row

    def test_family_ids_are_unique(self) -> None:
        assert new_family_id() != new_family_id()


class TestCsrf:
    def test_matching_tokens_pass(self) -> None:
        token = generate_csrf_token()
        assert csrf_tokens_match(token, token)

    def test_mismatched_tokens_fail(self) -> None:
        assert not csrf_tokens_match(generate_csrf_token(), generate_csrf_token())

    @pytest.mark.parametrize(
        ("cookie", "header"), [(None, "x"), ("x", None), (None, None), ("", "")]
    )
    def test_missing_tokens_fail(self, cookie: str | None, header: str | None) -> None:
        assert not csrf_tokens_match(cookie, header)


class TestKeyDerivation:
    def test_derivation_is_deterministic(self) -> None:
        assert derive_key(ROOT_SECRET, b"a") == derive_key(ROOT_SECRET, b"a")

    def test_labels_produce_independent_keys(self) -> None:
        assert derive_key(ROOT_SECRET, b"jwt") != derive_key(ROOT_SECRET, b"settings")

    def test_different_roots_produce_different_keys(self) -> None:
        assert derive_key(ROOT_SECRET, b"a") != derive_key("f" * 32, b"a")

    def test_empty_root_is_refused(self) -> None:
        with pytest.raises(ConfigurationError):
            derive_key("", b"a")


class TestSecretBox:
    def test_round_trip(self) -> None:
        box = SecretBox(ROOT_SECRET)
        assert box.decrypt(box.encrypt("123456:ABC-token")) == "123456:ABC-token"

    def test_ciphertext_hides_the_plaintext(self) -> None:
        box = SecretBox(ROOT_SECRET)
        assert "ABC-token" not in box.encrypt("123456:ABC-token")

    def test_encryption_is_non_deterministic(self) -> None:
        box = SecretBox(ROOT_SECRET)
        assert box.encrypt("same") != box.encrypt("same")

    def test_another_root_secret_cannot_decrypt(self) -> None:
        ciphertext = SecretBox(ROOT_SECRET).encrypt("secret")
        with pytest.raises(ConfigurationError, match="PROXSYNC_SECRET_KEY changed"):
            SecretBox("f" * 32).decrypt(ciphertext)

    def test_hint_masks_all_but_the_tail(self) -> None:
        box = SecretBox(ROOT_SECRET)
        assert box.hint("123456789:ABCDEF") == "••••••CDEF"
        assert box.hint("ab") == "••"


class TestSlidingWindowLimiter:
    def test_allows_up_to_the_limit(self) -> None:
        limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            assert limiter.check("key", now=100.0).allowed
            limiter.record("key", now=100.0)
        assert not limiter.check("key", now=100.0).allowed

    def test_window_expiry_restores_access(self) -> None:
        limiter = SlidingWindowLimiter(max_attempts=2, window_seconds=60)
        limiter.record("key", now=100.0)
        limiter.record("key", now=100.0)
        assert not limiter.check("key", now=120.0).allowed
        assert limiter.check("key", now=161.0).allowed

    def test_retry_after_is_reported(self) -> None:
        limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=60)
        limiter.record("key", now=100.0)
        assert limiter.check("key", now=110.0).retry_after_seconds == 50

    def test_keys_are_independent(self) -> None:
        limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=60)
        limiter.record("a", now=100.0)
        assert limiter.check("b", now=100.0).allowed

    def test_reset_clears_a_key(self) -> None:
        limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=60)
        limiter.record("a", now=100.0)
        limiter.reset("a")
        assert limiter.check("a", now=100.0).allowed

    def test_key_count_is_bounded(self) -> None:
        limiter = SlidingWindowLimiter(max_attempts=5, window_seconds=60, max_keys=10)
        for index in range(100):
            limiter.record(f"key-{index}", now=100.0)
        assert len(limiter) <= 10


class TestLockoutBackoff:
    def test_doubles_each_time(self) -> None:
        assert lockout_duration_seconds(1, base=60, maximum=3600) == 60
        assert lockout_duration_seconds(2, base=60, maximum=3600) == 120
        assert lockout_duration_seconds(3, base=60, maximum=3600) == 240

    def test_capped_at_the_maximum(self) -> None:
        assert lockout_duration_seconds(50, base=60, maximum=3600) == 3600

    def test_no_lockout_below_one(self) -> None:
        assert lockout_duration_seconds(0, base=60, maximum=3600) == 0


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30)
        assert breaker.state() is CircuitState.CLOSED
        assert breaker.allow()

    def test_opens_after_threshold_failures(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30)
        for _ in range(3):
            breaker.record_failure(now=100.0)
        assert breaker.state(now=100.0) is CircuitState.OPEN
        assert not breaker.allow(now=100.0)

    def test_stays_closed_below_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30)
        breaker.record_failure(now=100.0)
        breaker.record_failure(now=100.0)
        assert breaker.allow(now=100.0)

    def test_success_resets_the_counter(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_seconds=30)
        breaker.record_failure(now=100.0)
        breaker.record_success()
        breaker.record_failure(now=100.0)
        assert breaker.allow(now=100.0)

    def test_half_open_admits_one_probe(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30)
        breaker.record_failure(now=100.0)
        assert breaker.state(now=131.0) is CircuitState.HALF_OPEN
        assert breaker.allow(now=131.0)
        assert not breaker.allow(now=131.0)  # only one probe at a time

    def test_probe_success_closes_the_circuit(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30)
        breaker.record_failure(now=100.0)
        breaker.allow(now=131.0)
        breaker.record_success()
        assert breaker.state() is CircuitState.CLOSED

    def test_probe_failure_reopens_the_window(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30)
        breaker.record_failure(now=100.0)
        breaker.allow(now=131.0)
        breaker.record_failure(now=131.0)
        assert breaker.state(now=140.0) is CircuitState.OPEN

    def test_retry_after_counts_down(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30)
        breaker.record_failure(now=100.0)
        assert breaker.retry_after_seconds(now=110.0) == 20
