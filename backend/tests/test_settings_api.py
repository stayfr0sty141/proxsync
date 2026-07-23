"""Settings endpoints and secret handling."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.core.crypto import SecretBox
from app.repositories.settings_repository import SqlAlchemySettingsRepository, unwrap
from app.schemas.enums import SettingsSection, SettingValueType
from app.services.settings_service import SettingsService
from tests.conftest import SECRET_KEY, ApiClient

SETTINGS = "/api/v1/settings"


class TestReadSettings:
    def test_returns_every_section(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.get(SETTINGS)

        assert response.status_code == 200
        sections = {section["section"] for section in response.json()["sections"]}
        assert sections == {"general", "gdrive", "telegram", "retention", "agent", "proxmox"}

    def test_defaults_are_seeded_at_startup(self, authenticated_client: ApiClient) -> None:
        values = authenticated_client.get(f"{SETTINGS}/general").json()["values"]

        assert values["timezone"] == "Asia/Jakarta"
        assert values["storage_path"] == "/mnt/backup-hdd"
        assert values["backup_folder"] == "dump"

    def test_retention_defaults_match_the_specification(
        self, authenticated_client: ApiClient
    ) -> None:
        values = authenticated_client.get(f"{SETTINGS}/retention").json()["values"]

        assert values["keep_local"] == 2
        assert values["keep_remote"] == 2
        assert values["scope"] == "per_guest"
        assert values["require_upload_before_delete"] is True

    def test_unknown_section_is_rejected(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.get(f"{SETTINGS}/nonsense").status_code == 422


class TestWriteSettings:
    def test_updates_a_field(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.put(
            f"{SETTINGS}/retention", {"keep_local": 5, "keep_remote": 3}
        )

        assert response.status_code == 200
        assert response.json()["values"]["keep_local"] == 5
        assert (
            authenticated_client.get(f"{SETTINGS}/retention").json()["values"]["keep_remote"] == 3
        )

    def test_partial_update_leaves_other_fields_alone(
        self, authenticated_client: ApiClient
    ) -> None:
        authenticated_client.put(f"{SETTINGS}/general", {"log_retention_days": 30})
        values = authenticated_client.get(f"{SETTINGS}/general").json()["values"]

        assert values["log_retention_days"] == 30
        assert values["timezone"] == "Asia/Jakarta"  # untouched

    def test_unknown_field_is_rejected(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.put(f"{SETTINGS}/general", {"nope": 1})

        assert response.status_code == 400
        assert "Unknown setting" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("section", "payload"),
        [
            ("general", {"timezone": "Mars/Olympus"}),
            ("general", {"storage_path": "relative/path"}),
            ("general", {"backup_folder": "../escape"}),
            ("retention", {"keep_local": 0}),
            ("retention", {"storage_warning_percent": 90, "storage_critical_percent": 80}),
            ("gdrive", {"remote_name": "bad name!"}),
            ("gdrive", {"transfers": 0}),
            ("telegram", {"chat_id": "not-an-id"}),
            ("agent", {"poll_interval_seconds": 0}),
        ],
    )
    def test_invalid_values_are_rejected(
        self, authenticated_client: ApiClient, section: str, payload: dict[str, object]
    ) -> None:
        response = authenticated_client.put(f"{SETTINGS}/{section}", payload)
        assert response.status_code in {400, 422}

    def test_valid_values_are_accepted(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.put(f"{SETTINGS}/general", {"timezone": "Europe/Amsterdam"})
        assert response.status_code == 200
        assert response.json()["values"]["timezone"] == "Europe/Amsterdam"

    def test_a_rejected_write_changes_nothing(self, authenticated_client: ApiClient) -> None:
        before = authenticated_client.get(f"{SETTINGS}/retention").json()["values"]
        authenticated_client.put(f"{SETTINGS}/retention", {"keep_local": -1})
        after = authenticated_client.get(f"{SETTINGS}/retention").json()["values"]
        assert before == after

    def test_retention_relevant_change_queues_a_post_commit_rescan(
        self, authenticated_client: ApiClient
    ) -> None:
        worker = authenticated_client.raw.app.state.container.retention_worker  # type: ignore[attr-defined]
        queued = Mock()
        worker.notify = queued

        response = authenticated_client.put(f"{SETTINGS}/retention", {"keep_local": 4})

        assert response.status_code == 200
        queued.assert_called_once_with(None)


class TestSecrets:
    def test_secret_is_never_returned(self, authenticated_client: ApiClient) -> None:
        authenticated_client.put(
            f"{SETTINGS}/telegram", {"bot_token": "123456:AAH-secret-token", "chat_id": "-1001"}
        )

        response = authenticated_client.get(f"{SETTINGS}/telegram")
        body = response.json()

        assert "bot_token" not in body["values"]
        assert body["secrets"]["bot_token"]["configured"] is True
        assert body["secrets"]["bot_token"]["hint"] == "••••••oken"
        assert "AAH-secret-token" not in response.text

    def test_secret_is_encrypted_at_rest(self, authenticated_client: ApiClient) -> None:
        import sqlite3

        settings = authenticated_client.raw.app.state.container.settings  # type: ignore[attr-defined]
        authenticated_client.put(f"{SETTINGS}/telegram", {"bot_token": "123456:AAH-plaintext"})

        path = settings.database_url.split("///")[-1]
        connection = sqlite3.connect(path)
        rows = connection.execute(
            "SELECT value FROM settings WHERE section='telegram' AND key='bot_token'"
        ).fetchall()
        connection.close()

        assert rows and rows[0][0] is not None
        assert "AAH-plaintext" not in str(rows[0][0])

    def test_omitting_the_secret_keeps_it(self, authenticated_client: ApiClient) -> None:
        authenticated_client.put(f"{SETTINGS}/telegram", {"bot_token": "123456:AAH-keep-me"})
        authenticated_client.put(f"{SETTINGS}/telegram", {"chat_id": "-100999"})

        body = authenticated_client.get(f"{SETTINGS}/telegram").json()
        assert body["secrets"]["bot_token"]["configured"] is True
        assert body["values"]["chat_id"] == "-100999"

    def test_empty_string_clears_the_secret(self, authenticated_client: ApiClient) -> None:
        authenticated_client.put(f"{SETTINGS}/telegram", {"bot_token": "123456:AAH-clear-me"})
        authenticated_client.put(f"{SETTINGS}/telegram", {"bot_token": ""})

        body = authenticated_client.get(f"{SETTINGS}/telegram").json()
        assert body["secrets"]["bot_token"]["configured"] is False

    def test_sentinel_keeps_the_secret(self, authenticated_client: ApiClient) -> None:
        from app.schemas.settings import SECRET_UNCHANGED

        authenticated_client.put(f"{SETTINGS}/telegram", {"bot_token": "123456:AAH-sentinel"})
        authenticated_client.put(f"{SETTINGS}/telegram", {"bot_token": SECRET_UNCHANGED})

        body = authenticated_client.get(f"{SETTINGS}/telegram").json()
        assert body["secrets"]["bot_token"]["configured"] is True

    def test_connection_secrets_are_not_settings(self, authenticated_client: ApiClient) -> None:
        """The agent HMAC secret and Proxmox token live in the environment by design."""
        agent = authenticated_client.get(f"{SETTINGS}/agent").json()
        proxmox = authenticated_client.get(f"{SETTINGS}/proxmox").json()

        assert "hmac_secret" not in agent["values"]
        assert "base_url" not in agent["values"]
        assert "token_secret" not in proxmox["values"]


class TestAuthorisation:
    def test_settings_require_authentication(self, client: ApiClient) -> None:
        assert client.raw.get(SETTINGS).status_code == 401
        assert client.raw.put(f"{SETTINGS}/general", json={}).status_code == 401


async def test_startup_normalises_the_retired_unsafe_retention_value(
    session_factory: object,
) -> None:
    """An M4 database with the old false flag must still boot under M5's literal schema."""

    factory = session_factory
    async with factory() as session:  # type: ignore[operator]
        repository = SqlAlchemySettingsRepository(session)
        await repository.upsert(
            section=SettingsSection.RETENTION,
            key="require_upload_before_delete",
            value=False,
            value_type=SettingValueType.BOOL,
        )
        service = SettingsService(repository=repository, secret_box=SecretBox(SECRET_KEY))

        await service.ensure_defaults()

        setting = await repository.get(SettingsSection.RETENTION, "require_upload_before_delete")
        assert setting is not None
        assert unwrap(setting.value) is True
        section = await service.get_section(SettingsSection.RETENTION)
        assert section.require_upload_before_delete is True  # type: ignore[attr-defined]
