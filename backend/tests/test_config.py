"""Configuration loading regression tests.

The backend shares the agent's latent bug: pydantic-settings treats a ``list[...]`` field as
"complex" and ``json.loads()`` it at the source level — for both environment variables and the
``.env`` file — before any ``mode="before"`` validator runs. A bare value such as
``PROXSYNC_CORS_ORIGINS=https://ui.lan`` is not valid JSON, so it raised ``JSONDecodeError`` at
startup. The rest of the suite constructs ``Settings`` with keyword arguments and never touches
this path.

``enable_decoding=False`` plus ``_split_csv`` is the fix; these tests pin the CSV, JSON-list,
and empty forms of ``cors_origins`` so it cannot regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings

_SECRET = "x" * 32


class TestCorsOriginsFromEnvironment:
    def test_csv_string_populates_the_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROXSYNC_SECRET_KEY", _SECRET)
        monkeypatch.setenv("PROXSYNC_CORS_ORIGINS", "https://a.lan,https://b.lan")
        settings = Settings(_env_file=None)
        assert settings.cors_origins == ["https://a.lan", "https://b.lan"]

    def test_single_value_is_a_one_element_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROXSYNC_SECRET_KEY", _SECRET)
        monkeypatch.setenv("PROXSYNC_CORS_ORIGINS", "https://ui.lan")
        settings = Settings(_env_file=None)
        assert settings.cors_origins == ["https://ui.lan"]

    def test_json_list_form_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROXSYNC_SECRET_KEY", _SECRET)
        monkeypatch.setenv("PROXSYNC_CORS_ORIGINS", '["https://a.lan", "https://b.lan"]')
        settings = Settings(_env_file=None)
        assert settings.cors_origins == ["https://a.lan", "https://b.lan"]

    def test_empty_string_yields_the_default_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROXSYNC_SECRET_KEY", _SECRET)
        monkeypatch.setenv("PROXSYNC_CORS_ORIGINS", "")
        settings = Settings(_env_file=None)
        assert settings.cors_origins == []


class TestCorsOriginsFromEnvFile:
    def test_csv_value_loads_from_a_dotenv_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The production load path: env_file=".env", the DotEnv source that raised before the fix.
        env = tmp_path / ".env"
        env.write_text(
            "PROXSYNC_SECRET_KEY=" + _SECRET + "\nPROXSYNC_CORS_ORIGINS=https://ui.lan\n"
        )
        monkeypatch.delenv("PROXSYNC_CORS_ORIGINS", raising=False)
        settings = Settings(_env_file=str(env))
        assert settings.cors_origins == ["https://ui.lan"]
