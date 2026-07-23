"""Configuration loading regression tests.

These exist because of a bug that every other test missed: the suite builds ``AgentSettings``
by passing Python lists straight to the constructor (see ``conftest.py``), which never
exercises the path a real deployment uses — a CSV string in the environment or the ``.env``
file. pydantic-settings treats a ``list[...]`` field as "complex" and ``json.loads()`` it at
the source level before any ``mode="before"`` validator runs, so a bare value such as
``ALLOWED_BACKUP_STORAGES=backup-hdd`` raised ``JSONDecodeError`` at startup. The installer and
``.env.example`` both write exactly that format, so the agent would have crashed on first boot.

``enable_decoding=False`` plus ``_split_csv`` is the fix; these tests pin both the CSV and the
JSON-list forms so a future change to either cannot silently reintroduce the crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import AgentSettings


class TestListFieldsFromEnvironment:
    def test_csv_string_populates_a_list_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROXSYNC_AGENT_HMAC_SECRET", "x" * 32)
        monkeypatch.setenv("PROXSYNC_AGENT_ALLOWED_BACKUP_STORAGES", "backup-hdd,archive")
        settings = AgentSettings(_env_file=None)
        assert settings.allowed_backup_storages == ["backup-hdd", "archive"]

    def test_single_csv_value_is_a_one_element_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exact shape install-agent.sh writes: one bare token, no brackets, no quotes.
        monkeypatch.setenv("PROXSYNC_AGENT_HMAC_SECRET", "x" * 32)
        monkeypatch.setenv("PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS", "10.0.0.20/32")
        settings = AgentSettings(_env_file=None)
        assert settings.allowed_client_networks == ["10.0.0.20/32"]

    def test_json_list_form_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROXSYNC_AGENT_HMAC_SECRET", "x" * 32)
        monkeypatch.setenv("PROXSYNC_AGENT_ALLOWED_BACKUP_STORAGES", '["a", "b"]')
        settings = AgentSettings(_env_file=None)
        assert settings.allowed_backup_storages == ["a", "b"]

    def test_empty_string_yields_the_default_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROXSYNC_AGENT_HMAC_SECRET", "x" * 32)
        monkeypatch.setenv("PROXSYNC_AGENT_ALLOWED_RESTORE_STORAGES", "")
        settings = AgentSettings(_env_file=None)
        assert settings.allowed_restore_storages == []

    def test_csv_of_ints_populates_allowed_vmids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROXSYNC_AGENT_HMAC_SECRET", "x" * 32)
        monkeypatch.setenv("PROXSYNC_AGENT_ALLOWED_VMIDS", "100,101,102")
        settings = AgentSettings(_env_file=None)
        assert settings.allowed_vmids == [100, 101, 102]


class TestListFieldsFromEnvFile:
    def test_csv_values_load_from_a_dotenv_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The production load path: env_file=".env". This is the exact source (DotEnv) that
        # raised JSONDecodeError before the fix.
        env = tmp_path / ".env"
        env.write_text(
            "PROXSYNC_AGENT_HMAC_SECRET=" + "x" * 32 + "\n"
            "PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS=10.0.0.20/32\n"
            "PROXSYNC_AGENT_ALLOWED_BACKUP_STORAGES=backup-hdd\n"
        )
        for key in (
            "PROXSYNC_AGENT_ALLOWED_CLIENT_NETWORKS",
            "PROXSYNC_AGENT_ALLOWED_BACKUP_STORAGES",
        ):
            monkeypatch.delenv(key, raising=False)
        settings = AgentSettings(_env_file=str(env))
        assert settings.allowed_client_networks == ["10.0.0.20/32"]
        assert settings.allowed_backup_storages == ["backup-hdd"]
