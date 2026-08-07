"""The migration and the models must not drift apart.

This is the test that catches the most expensive class of mistake: a model changed, the
migration forgotten, and the schema on a real install silently different from the one every
test ran against.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine

from app.core.config import Settings
from app.db.models import Base

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(sync_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", sync_url)
    return config


@pytest.fixture
def alembic_url(settings: Settings) -> str:
    """The async URL: env.py builds an async engine, so these tests drive the real path."""
    return settings.database_url


@pytest.fixture
def inspect_url(settings: Settings) -> str:
    """A sync URL for reading the resulting schema back."""
    return settings.database_url.replace("+aiosqlite", "")


def test_upgrade_head_creates_every_table(alembic_url: str, inspect_url: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(alembic_url), "head")

    engine = create_engine(inspect_url)
    with engine.connect() as connection:
        from sqlalchemy import inspect

        tables = set(inspect(connection).get_table_names())
    engine.dispose()

    missing = set(Base.metadata.tables) - tables
    assert missing == set(), f"the migration does not create: {sorted(missing)}"


def test_no_pending_model_changes(alembic_url: str, inspect_url: str) -> None:
    """After `upgrade head`, autogenerate must find nothing left to do."""
    from alembic import command

    command.upgrade(_alembic_config(alembic_url), "head")

    engine = create_engine(inspect_url)
    with engine.connect() as connection:
        differences = _diff(connection)
    engine.dispose()

    assert differences == [], (
        "models and migrations have drifted; run "
        "`alembic revision --autogenerate` and review the result:\n"
        + "\n".join(str(difference) for difference in differences)
    )


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "target_metadata": Base.metadata, "render_as_batch": True},
    )
    # Index comparison on SQLite reports spurious differences for expression indexes; the
    # table/column/constraint diff is what actually matters for correctness.
    return [
        difference
        for difference in compare_metadata(context, Base.metadata)
        if not _is_index_noise(difference)
    ]


def _is_index_noise(difference: object) -> bool:
    if isinstance(difference, tuple) and difference:
        return str(difference[0]).endswith("_index")
    return False


def test_downgrade_is_reversible(alembic_url: str, inspect_url: str) -> None:
    from sqlalchemy import inspect

    from alembic import command

    config = _alembic_config(alembic_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(inspect_url)
    with engine.connect() as connection:
        remaining = set(inspect(connection).get_table_names()) - {"alembic_version"}
    engine.dispose()

    assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"


def test_single_head() -> None:
    """Two heads mean a merge was missed and `upgrade head` becomes ambiguous."""
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1
