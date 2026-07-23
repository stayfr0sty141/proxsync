"""Authentication through the full HTTP stack."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from tests.conftest import ADMIN_PASSWORD, ApiClient

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
CHANGE_PASSWORD = "/api/v1/auth/change-password"
SESSIONS = "/api/v1/auth/sessions"


class TestLogin:
    def test_bootstrap_admin_can_sign_in(self, client: ApiClient) -> None:
        response = client.login()

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900
        assert body["user"]["username"] == "admin"
        assert body["user"]["role"] == "admin"
        assert body["user"]["must_change_password"] is True

    def test_sets_httponly_refresh_and_readable_csrf_cookies(self, client: ApiClient) -> None:
        response = client.login()

        cookies = {cookie["name"]: cookie for cookie in _parse_set_cookie(response)}
        assert cookies["proxsync_refresh"]["httponly"] is True
        assert str(cookies["proxsync_refresh"]["samesite"]).lower() == "strict"
        assert cookies["proxsync_refresh"]["path"] == "/api/v1/auth"
        assert cookies["proxsync_csrf"]["httponly"] is False

    def test_access_token_is_not_in_a_cookie(self, client: ApiClient) -> None:
        response = client.login()
        names = {cookie["name"] for cookie in _parse_set_cookie(response)}
        assert "proxsync_access" not in names
        assert response.json()["access_token"] not in response.headers.get("set-cookie", "")

    def test_wrong_password_is_rejected(self, client: ApiClient) -> None:
        response = client.raw.post(LOGIN, json={"username": "admin", "password": "nope"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid username or password"

    def test_unknown_user_gives_the_same_message(self, client: ApiClient) -> None:
        unknown = client.raw.post(LOGIN, json={"username": "nobody", "password": "nope"})
        known = client.raw.post(LOGIN, json={"username": "admin", "password": "nope"})

        # Identical response: an attacker must not be able to enumerate usernames.
        assert unknown.status_code == known.status_code == 401
        assert unknown.json()["detail"] == known.json()["detail"]

    def test_username_is_case_insensitive(self, client: ApiClient) -> None:
        response = client.raw.post(LOGIN, json={"username": "ADMIN", "password": ADMIN_PASSWORD})
        assert response.status_code == 200

    def test_account_locks_after_repeated_failures(self, client: ApiClient) -> None:
        for _ in range(5):
            client.raw.post(LOGIN, json={"username": "admin", "password": "wrong"})

        response = client.raw.post(LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})

        # 423 (locked) or 429 (rate limited) — either way the correct password is refused.
        assert response.status_code in {423, 429}

    def test_lockout_survives_the_correct_password(self, client: ApiClient) -> None:
        for _ in range(6):
            client.raw.post(LOGIN, json={"username": "admin", "password": "wrong"})
        response = client.raw.post(LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})
        assert response.status_code != 200

    def test_rate_limit_reports_retry_after(self, client: ApiClient) -> None:
        last = None
        for _ in range(8):
            last = client.raw.post(LOGIN, json={"username": "admin", "password": "wrong"})

        assert last is not None
        if last.status_code == 429:
            assert "Retry-After" in last.headers
            assert last.json()["retry_after"] > 0

    def test_malformed_body_is_rejected(self, client: ApiClient) -> None:
        assert client.raw.post(LOGIN, json={"username": "admin"}).status_code == 422
        assert (
            client.raw.post(LOGIN, json={"username": "a", "password": "b", "x": 1}).status_code
            == 422
        )


class TestRefresh:
    def test_rotates_the_refresh_token(self, client: ApiClient) -> None:
        client.login()
        original = client.cookies.get("proxsync_refresh")

        response = client.post(REFRESH)

        assert response.status_code == 200
        assert client.cookies.get("proxsync_refresh") != original
        assert response.json()["access_token"]

    def test_requires_the_csrf_header(self, client: ApiClient) -> None:
        client.login()
        response = client.raw.post(REFRESH)  # cookies sent, header absent

        assert response.status_code == 403
        assert response.json()["type"].endswith("csrf-failed")

    def test_rejects_a_mismatched_csrf_header(self, client: ApiClient) -> None:
        client.login()
        response = client.raw.post(REFRESH, headers={"X-CSRF-Token": "not-the-token"})
        assert response.status_code == 403

    def test_without_a_cookie_is_unauthenticated(self, client: ApiClient) -> None:
        client.login()
        client.raw.cookies.delete("proxsync_refresh")
        response = client.post(REFRESH)
        assert response.status_code == 401

    def test_reusing_a_rotated_token_revokes_the_family(self, client: ApiClient) -> None:
        client.login()
        stolen = client.cookies.get("proxsync_refresh")
        client.post(REFRESH)  # rotates; `stolen` is now revoked

        client.raw.cookies.set("proxsync_refresh", stolen, path="/api/v1/auth")
        replay = client.post(REFRESH)

        assert replay.status_code == 401
        assert "reused" in replay.json()["detail"]

        # The legitimate client is signed out too — the family is burned deliberately.
        assert client.post(REFRESH).status_code == 401


class TestCurrentUser:
    def test_returns_the_signed_in_user(self, client: ApiClient) -> None:
        client.login()
        response = client.get(ME)

        assert response.status_code == 200
        assert response.json()["username"] == "admin"

    def test_requires_a_token(self, client: ApiClient) -> None:
        assert client.raw.get(ME).status_code == 401

    def test_rejects_a_malformed_authorization_header(self, client: ApiClient) -> None:
        response = client.raw.get(ME, headers={"Authorization": "Basic abc"})
        assert response.status_code == 401

    def test_rejects_a_garbage_token(self, client: ApiClient) -> None:
        response = client.raw.get(ME, headers={"Authorization": "Bearer not.a.jwt"})
        assert response.status_code == 401


class TestChangePassword:
    def test_changes_the_password_and_clears_the_flag(self, client: ApiClient) -> None:
        client.login()
        response = client.post(
            CHANGE_PASSWORD,
            {"current_password": ADMIN_PASSWORD, "new_password": "a-much-better-password-2"},
        )

        assert response.status_code == 200
        assert client.login(password="a-much-better-password-2").status_code == 200
        assert client.get(ME).json()["must_change_password"] is False

    def test_old_password_stops_working(self, client: ApiClient) -> None:
        client.login()
        client.post(
            CHANGE_PASSWORD,
            {"current_password": ADMIN_PASSWORD, "new_password": "a-much-better-password-2"},
        )
        assert (
            client.raw.post(
                LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD}
            ).status_code
            == 401
        )

    def test_wrong_current_password_is_rejected(self, client: ApiClient) -> None:
        client.login()
        response = client.post(
            CHANGE_PASSWORD,
            {"current_password": "wrong", "new_password": "a-much-better-password-2"},
        )
        assert response.status_code == 401

    def test_short_password_is_rejected(self, client: ApiClient) -> None:
        client.login()
        response = client.post(
            CHANGE_PASSWORD, {"current_password": ADMIN_PASSWORD, "new_password": "short"}
        )
        assert response.status_code == 422

    def test_reusing_the_same_password_is_rejected(self, client: ApiClient) -> None:
        client.login()
        response = client.post(
            CHANGE_PASSWORD,
            {"current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD},
        )
        assert response.status_code in {400, 422}

    def test_all_sessions_are_revoked(self, client: ApiClient) -> None:
        client.login()
        refresh_cookie = client.cookies.get("proxsync_refresh")
        csrf_cookie = client.cookies.get("proxsync_csrf")

        client.post(
            CHANGE_PASSWORD,
            {"current_password": ADMIN_PASSWORD, "new_password": "a-much-better-password-2"},
        )

        # Replaying the pre-change session must fail: that is the point of the revocation.
        client.raw.cookies.set("proxsync_refresh", refresh_cookie, path="/api/v1/auth")
        client.raw.cookies.set("proxsync_csrf", csrf_cookie, path="/")
        assert client.post(REFRESH).status_code == 401


class TestForcedPasswordChange:
    def test_other_endpoints_are_blocked_until_the_password_changes(
        self, client: ApiClient
    ) -> None:
        client.login()
        response = client.get("/api/v1/settings")

        assert response.status_code == 403
        assert "change your password" in response.json()["detail"].lower()

    def test_endpoints_open_up_afterwards(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.get("/api/v1/settings").status_code == 200


class TestSessions:
    def test_lists_the_current_session(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.get(SESSIONS)

        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) >= 1
        assert any(session["current"] for session in sessions)

    def test_revoking_a_session_ends_it(self, authenticated_client: ApiClient) -> None:
        session_id = authenticated_client.get(SESSIONS).json()[0]["id"]

        response = authenticated_client.delete(f"{SESSIONS}/{session_id}")

        assert response.status_code == 200
        assert authenticated_client.post(REFRESH).status_code == 401

    def test_revoking_an_unknown_session_is_rejected(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.delete(f"{SESSIONS}/999999").status_code == 400


class TestLogout:
    def test_clears_cookies_and_revokes_the_family(self, client: ApiClient) -> None:
        client.login()
        refresh_cookie = client.cookies.get("proxsync_refresh")
        csrf_cookie = client.cookies.get("proxsync_csrf")

        response = client.post(LOGOUT)

        assert response.status_code == 200
        assert client.cookies.get("proxsync_refresh") is None

        # Restore the cookies a thief would have kept: the family must still be dead.
        client.raw.cookies.set("proxsync_refresh", refresh_cookie, path="/api/v1/auth")
        client.raw.cookies.set("proxsync_csrf", csrf_cookie, path="/")
        assert client.post(REFRESH).status_code == 401

    def test_requires_csrf(self, client: ApiClient) -> None:
        client.login()
        assert client.raw.post(LOGOUT).status_code == 403


class TestBootstrap:
    async def test_no_admin_is_created_without_a_password(self, settings: Settings) -> None:
        """A fresh install without PROXSYNC_BOOTSTRAP_ADMIN_PASSWORD must not invent an
        account with a guessable password."""
        from fastapi.testclient import TestClient
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.db.models import Base
        from app.main import create_app

        settings.bootstrap_admin_password = None
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

        with TestClient(create_app(settings)) as test_client:
            response = test_client.post(LOGIN, json={"username": "admin", "password": "admin"})
            assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/settings", "/api/v1/health/detail"])
def test_protected_endpoints_require_authentication(client: ApiClient, path: str) -> None:
    assert client.raw.get(path).status_code == 401


def test_health_is_public(client: ApiClient) -> None:
    response = client.raw.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # Liveness must not describe the deployment to an unauthenticated caller.
    assert set(response.json()) == {"status", "version"}


def test_security_headers_are_present(client: ApiClient) -> None:
    response = client.raw.get("/api/v1/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_correlation_id_is_echoed(client: ApiClient) -> None:
    response = client.raw.get("/api/v1/health", headers={"X-Correlation-ID": "trace-me-123"})
    assert response.headers["X-Correlation-ID"] == "trace-me-123"


def test_correlation_id_is_generated_when_absent(client: ApiClient) -> None:
    assert client.raw.get("/api/v1/health").headers["X-Correlation-ID"]


def _parse_set_cookie(response: object) -> list[dict[str, object]]:
    """Parse Set-Cookie headers into dicts of attributes."""
    raw_headers = response.headers.get_list("set-cookie")  # type: ignore[attr-defined]
    parsed: list[dict[str, object]] = []
    for header in raw_headers:
        parts = [part.strip() for part in header.split(";")]
        name, _, value = parts[0].partition("=")
        attributes: dict[str, object] = {
            "name": name,
            "value": value,
            "httponly": False,
            "secure": False,
            "samesite": "",
            "path": "",
        }
        for part in parts[1:]:
            key, _, attribute_value = part.partition("=")
            key_lower = key.lower()
            if key_lower == "httponly":
                attributes["httponly"] = True
            elif key_lower == "secure":
                attributes["secure"] = True
            elif key_lower in {"samesite", "path", "domain", "max-age"}:
                attributes[key_lower] = attribute_value
        parsed.append(attributes)
    return parsed
