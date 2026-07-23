"""Password hashing, access tokens, refresh tokens and CSRF tokens.

Design notes that matter more than the code:

* **Refresh tokens are not JWTs.** They are opaque random strings stored only as SHA-256
  digests, so a database read cannot mint a session. Each belongs to a *family*; presenting a
  token that was already rotated revokes the whole family, which turns token theft into a
  detectable, self-limiting event.
* **Access tokens are JWTs** with a 15-minute life, held in browser memory only. They are
  never written to a cookie, so they cannot be replayed by a CSRF.
* **Login timing is equalised.** An unknown username performs a dummy Argon2 verification, so
  response time does not reveal whether an account exists.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import AuthenticationFailed

REFRESH_TOKEN_BYTES = 48
CSRF_TOKEN_BYTES = 32

# Verified when the username does not exist, purely to spend the same time as a real check.
_DUMMY_PASSWORD = "proxsync-timing-equalisation"  # noqa: S105 - not a credential


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: int
    username: str
    role: str
    jti: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GeneratedRefreshToken:
    raw: str
    """Returned to the client once, in an HttpOnly cookie. Never persisted."""
    digest: str
    """SHA-256 hex of ``raw`` — this is what the database stores."""


class PasswordService:
    def __init__(self, *, time_cost: int, memory_cost: int, parallelism: int) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism
        )
        self._dummy_hash = self._hasher.hash(_DUMMY_PASSWORD)

    def hash(self, password: str) -> str:
        return str(self._hasher.hash(password))

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        return True

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return bool(self._hasher.check_needs_rehash(password_hash))
        except InvalidHashError:
            return True

    def dummy_verify(self) -> None:
        """Spend a verification's worth of time so unknown usernames are indistinguishable."""
        self.verify(self._dummy_hash, "wrong-password")


class TokenService:
    def __init__(
        self,
        *,
        signing_key: str,
        algorithm: str,
        issuer: str,
        audience: str,
        access_ttl_seconds: int,
        refresh_ttl_seconds: int,
    ) -> None:
        self._key = signing_key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._access_ttl = timedelta(seconds=access_ttl_seconds)
        self._refresh_ttl = timedelta(seconds=refresh_ttl_seconds)

    @property
    def access_ttl_seconds(self) -> int:
        return int(self._access_ttl.total_seconds())

    @property
    def refresh_ttl_seconds(self) -> int:
        return int(self._refresh_ttl.total_seconds())

    def create_access_token(
        self, *, user_id: int, username: str, role: str, now: datetime | None = None
    ) -> tuple[str, AccessTokenClaims]:
        issued = now or datetime.now(UTC)
        expires = issued + self._access_ttl
        jti = secrets.token_urlsafe(16)
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "username": username,
            "role": role,
            "jti": jti,
            "typ": "access",
            "iat": int(issued.timestamp()),
            "nbf": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
            "iss": self._issuer,
            "aud": self._audience,
        }
        token = jwt.encode(payload, self._key, algorithm=self._algorithm)
        claims = AccessTokenClaims(
            user_id=user_id,
            username=username,
            role=role,
            jti=jti,
            issued_at=issued,
            expires_at=expires,
        )
        return token, claims

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationFailed("Access token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationFailed("Access token is invalid") from exc

        if payload.get("typ") != "access":
            raise AuthenticationFailed("Token is not an access token")

        try:
            user_id = int(payload["sub"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationFailed("Access token has no usable subject") from exc

        return AccessTokenClaims(
            user_id=user_id,
            username=str(payload.get("username", "")),
            role=str(payload.get("role", "")),
            jti=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

    def generate_refresh_token(self) -> GeneratedRefreshToken:
        raw = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
        return GeneratedRefreshToken(raw=raw, digest=hash_token(raw))

    def refresh_expiry(self, now: datetime | None = None) -> datetime:
        return (now or datetime.now(UTC)) + self._refresh_ttl


def hash_token(raw: str) -> str:
    """SHA-256 hex. Refresh tokens are high-entropy random strings, so a fast hash is correct
    here — key stretching protects low-entropy secrets, which these are not."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def csrf_tokens_match(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


def new_family_id() -> str:
    return secrets.token_urlsafe(16)
