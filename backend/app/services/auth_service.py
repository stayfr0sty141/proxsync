"""Authentication use cases.

Threat decisions encoded here rather than left to the routes:

* A failed login and an unknown username are indistinguishable — same message, same timing
  (an unknown user still pays for one Argon2 verification).
* Lockout is per *account* and exponential, so an attacker rotating source addresses gains
  nothing; the sliding-window limiter separately stops one address spraying one account.
* Presenting an already-rotated refresh token revokes its entire family. Legitimate clients
  never do this, so it is treated as evidence of theft rather than as a retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.errors import AccountLocked, AuthenticationFailed, RateLimited, ValidationFailed
from app.core.logging import logger
from app.core.rate_limit import SlidingWindowLimiter, lockout_duration_seconds
from app.core.security import (
    GeneratedRefreshToken,
    PasswordService,
    TokenService,
    generate_csrf_token,
    hash_token,
    new_family_id,
)
from app.db.models.user import RefreshToken, User
from app.repositories.audit_repository import SqlAlchemyAuditRepository
from app.repositories.refresh_token_repository import SqlAlchemyRefreshTokenRepository
from app.repositories.user_repository import SqlAlchemyUserRepository, normalise_username
from app.schemas.auth import MIN_PASSWORD_LENGTH
from app.schemas.enums import AuditAction, AuditResult, UserRole

GENERIC_LOGIN_FAILURE = "Invalid username or password"


@dataclass(frozen=True, slots=True)
class RequestContext:
    ip_address: str | None
    user_agent: str | None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    csrf_token: str
    family_id: str
    user: User


class AuthService:
    def __init__(
        self,
        *,
        settings: Settings,
        users: SqlAlchemyUserRepository,
        refresh_tokens: SqlAlchemyRefreshTokenRepository,
        audit: SqlAlchemyAuditRepository,
        passwords: PasswordService,
        tokens: TokenService,
        limiter: SlidingWindowLimiter,
    ) -> None:
        self._settings = settings
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._audit = audit
        self._passwords = passwords
        self._tokens = tokens
        self._limiter = limiter

    # ---- login ---------------------------------------------------------------

    async def login(self, username: str, password: str, context: RequestContext) -> IssuedSession:
        normalised = normalise_username(username)
        limiter_key = f"{context.ip_address or 'unknown'}|{normalised}"

        decision = self._limiter.check(limiter_key)
        if not decision.allowed:
            await self._audit.record(
                action=AuditAction.LOGIN_FAILURE,
                result=AuditResult.FAILURE,
                username=normalised,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
                detail={"reason": "rate_limited"},
            )
            raise RateLimited(
                "Too many login attempts. Try again shortly.",
                retry_after=decision.retry_after_seconds,
            )

        user = await self._users.get_by_username(normalised)
        now = datetime.now(UTC)

        if user is None:
            # Spend a verification so an unknown username is not faster than a wrong password.
            self._passwords.dummy_verify()
            self._limiter.record(limiter_key)
            await self._audit.record(
                action=AuditAction.LOGIN_FAILURE,
                result=AuditResult.FAILURE,
                username=normalised,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
                detail={"reason": "unknown_user"},
            )
            raise AuthenticationFailed(GENERIC_LOGIN_FAILURE)

        if user.locked_until is not None and user.locked_until > now:
            remaining = int((user.locked_until - now).total_seconds())
            await self._audit.record(
                action=AuditAction.LOGIN_FAILURE,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
                detail={"reason": "locked", "remaining_seconds": remaining},
            )
            raise AccountLocked(
                f"This account is locked for another {remaining} seconds "
                "after repeated failed sign-ins."
            )

        if not self._passwords.verify(user.password_hash, password):
            await self._register_failure(user, limiter_key, context, now)
            raise AuthenticationFailed(GENERIC_LOGIN_FAILURE)

        # Correct credentials from here on, so a specific message leaks nothing.
        if not user.is_active:
            await self._audit.record(
                action=AuditAction.LOGIN_FAILURE,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
                detail={"reason": "inactive"},
            )
            raise AuthenticationFailed("This account has been disabled.")

        if self._passwords.needs_rehash(user.password_hash):
            # Argon2 parameters were raised since this password was set; upgrade transparently.
            user.password_hash = self._passwords.hash(password)
            logger.info("password_rehashed", user_id=user.id)

        self._limiter.reset(limiter_key)
        await self._users.record_login_success(user, at=now, ip=context.ip_address)
        await self._audit.record(
            action=AuditAction.LOGIN_SUCCESS,
            user_id=user.id,
            username=user.username,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        )
        return await self._issue_session(user, family_id=new_family_id(), context=context, now=now)

    async def _register_failure(
        self, user: User, limiter_key: str, context: RequestContext, now: datetime
    ) -> None:
        self._limiter.record(limiter_key)
        failed_count = user.failed_login_count + 1
        locked_until = None

        if failed_count >= self._settings.login_max_attempts:
            over_limit = failed_count - self._settings.login_max_attempts + 1
            seconds = lockout_duration_seconds(
                over_limit,
                base=self._settings.login_lockout_base_seconds,
                maximum=self._settings.login_lockout_max_seconds,
            )
            locked_until = now + timedelta(seconds=seconds)
            await self._audit.record(
                action=AuditAction.ACCOUNT_LOCKED,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                ip_address=context.ip_address,
                detail={"failed_count": failed_count, "lock_seconds": seconds},
            )
            logger.warning(
                "account_locked", user_id=user.id, failed_count=failed_count, seconds=seconds
            )

        await self._users.record_login_failure(
            user, failed_count=failed_count, locked_until=locked_until
        )
        await self._audit.record(
            action=AuditAction.LOGIN_FAILURE,
            result=AuditResult.FAILURE,
            user_id=user.id,
            username=user.username,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            detail={"reason": "bad_password", "failed_count": failed_count},
        )

    # ---- refresh -------------------------------------------------------------

    async def refresh(self, raw_token: str, context: RequestContext) -> IssuedSession:
        now = datetime.now(UTC)
        stored = await self._refresh_tokens.get_by_hash(hash_token(raw_token))

        if stored is None:
            raise AuthenticationFailed("Refresh token is not recognised. Sign in again.")

        if stored.revoked_at is not None:
            # A rotated token being replayed means the token was captured. Burn the family.
            revoked = await self._refresh_tokens.revoke_family(
                stored.family_id, at=now, reason="reuse_detected"
            )
            await self._audit.record(
                action=AuditAction.TOKEN_REUSE_DETECTED,
                result=AuditResult.FAILURE,
                user_id=stored.user_id,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
                detail={"family_id": stored.family_id, "revoked_tokens": revoked},
            )
            logger.warning(
                "refresh_token_reuse", user_id=stored.user_id, family_id=stored.family_id
            )
            raise AuthenticationFailed(
                "This session has been revoked because a token was reused. Sign in again."
            )

        if stored.expires_at <= now:
            await self._refresh_tokens.revoke(stored, at=now, reason="expired")
            raise AuthenticationFailed("Session has expired. Sign in again.")

        user = await self._users.get(stored.user_id)
        if user is None or not user.is_active:
            await self._refresh_tokens.revoke_family(
                stored.family_id, at=now, reason="user_unavailable"
            )
            raise AuthenticationFailed("This account is no longer available.")

        await self._refresh_tokens.revoke(stored, at=now, reason="rotated")
        await self._audit.record(
            action=AuditAction.TOKEN_REFRESH,
            user_id=user.id,
            username=user.username,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        )
        return await self._issue_session(user, family_id=stored.family_id, context=context, now=now)

    async def _issue_session(
        self, user: User, *, family_id: str, context: RequestContext, now: datetime
    ) -> IssuedSession:
        access_token, claims = self._tokens.create_access_token(
            user_id=user.id, username=user.username, role=user.role, now=now
        )
        generated: GeneratedRefreshToken = self._tokens.generate_refresh_token()
        expires_at = self._tokens.refresh_expiry(now)

        await self._refresh_tokens.create(
            user_id=user.id,
            token_hash=generated.digest,
            family_id=family_id,
            expires_at=expires_at,
            created_at=now,
            user_agent=context.user_agent,
            ip_address=context.ip_address,
        )

        return IssuedSession(
            access_token=access_token,
            expires_in=int((claims.expires_at - now).total_seconds()),
            refresh_token=generated.raw,
            refresh_expires_at=expires_at,
            csrf_token=generate_csrf_token(),
            family_id=family_id,
            user=user,
        )

    # ---- logout and sessions -------------------------------------------------

    async def logout(self, raw_token: str | None, context: RequestContext) -> None:
        """Revoke the presented session's whole family. Always succeeds."""
        if not raw_token:
            return
        stored = await self._refresh_tokens.get_by_hash(hash_token(raw_token))
        if stored is None:
            return
        now = datetime.now(UTC)
        await self._refresh_tokens.revoke_family(stored.family_id, at=now, reason="logout")
        await self._audit.record(
            action=AuditAction.LOGOUT,
            user_id=stored.user_id,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
        )

    async def list_sessions(self, user_id: int) -> list[RefreshToken]:
        return await self._refresh_tokens.list_active_for_user(user_id, now=datetime.now(UTC))

    async def revoke_session(self, user_id: int, session_id: int) -> None:
        sessions = await self.list_sessions(user_id)
        target = next((session for session in sessions if session.id == session_id), None)
        if target is None:
            raise ValidationFailed(f"No active session with id {session_id}")
        await self._refresh_tokens.revoke_family(
            target.family_id, at=datetime.now(UTC), reason="revoked_by_user"
        )

    # ---- password ------------------------------------------------------------

    async def change_password(
        self, user: User, current_password: str, new_password: str, context: RequestContext
    ) -> None:
        if not self._passwords.verify(user.password_hash, current_password):
            await self._audit.record(
                action=AuditAction.PASSWORD_CHANGED,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                ip_address=context.ip_address,
                detail={"reason": "current_password_incorrect"},
            )
            raise AuthenticationFailed("Current password is incorrect")

        if new_password == current_password:
            raise ValidationFailed("The new password must differ from the current one")

        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise ValidationFailed(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

        user.password_hash = self._passwords.hash(new_password)
        user.must_change_password = False

        # Every other session is invalidated: a password change is how a user responds to
        # suspected compromise, and leaving old sessions alive would defeat that.
        revoked = await self._refresh_tokens.revoke_all_for_user(
            user.id, at=datetime.now(UTC), reason="password_changed"
        )
        await self._audit.record(
            action=AuditAction.PASSWORD_CHANGED,
            user_id=user.id,
            username=user.username,
            ip_address=context.ip_address,
            detail={"sessions_revoked": revoked},
        )

    # ---- bootstrap -----------------------------------------------------------

    async def ensure_bootstrap_admin(self) -> User | None:
        """Create the first admin when the user table is empty. Idempotent."""
        if await self._users.count() > 0:
            return None

        password = self._settings.bootstrap_admin_password
        if password is None:
            logger.warning(
                "bootstrap_admin_skipped",
                detail="No users exist and PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD is unset. "
                "Set it once and restart to create the first administrator.",
            )
            return None

        user = await self._users.create(
            username=self._settings.bootstrap_admin_username,
            password_hash=self._passwords.hash(password.get_secret_value()),
            role=UserRole.ADMIN,
            must_change_password=True,
        )
        await self._audit.record(
            action=AuditAction.USER_CREATED,
            user_id=user.id,
            username=user.username,
            detail={"bootstrap": True, "role": UserRole.ADMIN.value},
        )
        logger.info("bootstrap_admin_created", username=user.username)
        return user
