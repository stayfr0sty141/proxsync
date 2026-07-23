"""The persistence side: batching, failure handling and retention."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.log_sink import LogSink
from app.db.session import Database
from app.repositories.audit_repository import SqlAlchemyAuditRepository
from app.repositories.log_repository import NewLogEntry, SqlAlchemyLogRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import AuditAction, LogCategory, LogLevel, SettingsSection
from app.services.settings_service import SettingsService
from app.workers.log_writer import LogWriter

from .conftest import SECRET_KEY


@pytest.fixture
async def database(settings: Settings, prepared_database: None) -> AsyncIterator[Database]:
    del prepared_database
    instance = Database(settings)
    yield instance
    await instance.dispose()


@pytest.fixture
def sink() -> LogSink:
    instance = LogSink()
    instance.configure(capacity=100, level=LogLevel.INFO, enabled=True)
    return instance


@pytest.fixture
def writer(settings: Settings, database: Database, sink: LogSink) -> LogWriter:
    fast = settings.model_copy(
        update={"log_flush_interval_seconds": 0.01, "log_flush_batch_size": 3}
    )
    return LogWriter(database=database, settings=fast, secret_box=SecretBox(SECRET_KEY), sink=sink)


def emit(sink: LogSink, event: str = "backup_finished", **fields: object) -> None:
    sink.capture(
        None,
        "info",
        {
            "event": event,
            "level": "info",
            "timestamp": datetime.now(UTC).isoformat(),
            **fields,
        },
    )


async def stored_messages(database: Database) -> list[str]:
    async with database.session() as session:
        rows = await SqlAlchemyLogRepository(session).search(limit=100)
    return [row.message for row in rows]


# ---- flushing ----------------------------------------------------------------


async def test_a_captured_entry_becomes_a_log_row(
    writer: LogWriter, database: Database, sink: LogSink
) -> None:
    emit(sink, "backup_finished", vmid=101, correlation_id="abc")

    written = await writer.flush_once()

    assert written == 1
    async with database.session() as session:
        row = (await SqlAlchemyLogRepository(session).search(limit=1))[0]
    assert row.message == "backup_finished"
    assert row.category == LogCategory.BACKUP.value
    assert row.correlation_id == "abc"
    assert row.context == {"vmid": 101}


async def test_a_flush_takes_at_most_one_batch(
    writer: LogWriter, database: Database, sink: LogSink
) -> None:
    for index in range(5):
        emit(sink, f"line_{index}")

    first = await writer.flush_once()
    second = await writer.flush_once()

    assert (first, second) == (3, 2)
    assert sorted(await stored_messages(database)) == [f"line_{i}" for i in range(5)]


async def test_flushing_an_empty_buffer_is_free(writer: LogWriter) -> None:
    assert await writer.flush_once() == 0


async def test_a_line_naming_a_row_that_does_not_exist_is_still_written(
    writer: LogWriter, database: Database, sink: LogSink
) -> None:
    """The line is the point, not the join. A backup id from a rolled-back transaction must
    not wedge every log row behind one poisoned batch."""
    emit(sink, "backup_finished", backup_id=999999)

    written = await writer.flush_once()

    assert written == 1
    async with database.session() as session:
        row = (await SqlAlchemyLogRepository(session).search(limit=1))[0]
    assert row.backup_id is None
    # The id survives where the foreign key could not.
    assert row.context == {"backup_id": 999999}


async def test_a_failed_batch_is_put_back_rather_than_lost(
    writer: LogWriter, sink: LogSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database hiccup must cost latency, not log lines."""

    async def unavailable(_entries: list[NewLogEntry]) -> None:
        raise OperationalError("INSERT INTO logs", {}, Exception("database is locked"))

    emit(sink, "line_a")
    monkeypatch.setattr(writer, "_insert", unavailable)

    with pytest.raises(OperationalError):
        await writer.flush_once()

    assert sink.pending == 1
    assert writer.last_error is not None
    # And the line is still there for the next attempt, not merely counted.
    assert sink.drain(1)[0].message == "line_a"


async def test_the_writer_does_not_log_itself_into_a_loop(writer: LogWriter, sink: LogSink) -> None:
    """Capture is off for the whole write, so SQLAlchemy noise and the writer's own lines
    cannot refill the queue it is draining."""
    emit(sink, "line_a")

    await writer.flush_once()

    assert sink.pending == 0


# ---- the loop ----------------------------------------------------------------


async def test_the_running_writer_persists_what_is_captured(
    writer: LogWriter, database: Database, sink: LogSink
) -> None:
    await writer.start()
    try:
        emit(sink, "captured_while_running")
        for _ in range(200):
            if "captured_while_running" in await stored_messages(database):
                break
            await asyncio.sleep(0.01)
    finally:
        await writer.stop()

    assert "captured_while_running" in await stored_messages(database)


async def test_stopping_flushes_what_shutdown_logged(
    writer: LogWriter, database: Database, sink: LogSink
) -> None:
    """The lines explaining why the process is stopping are the ones most worth keeping."""
    await writer.start()
    emit(sink, "api_stopping")
    await writer.stop()

    assert "api_stopping" in await stored_messages(database)


# ---- retention ---------------------------------------------------------------


async def seed_settings(database: Database, **overrides: object) -> None:
    async with database.session() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        if overrides:
            await service.update_section(SettingsSection.GENERAL, dict(overrides))


async def test_pruning_removes_logs_past_their_retention_and_keeps_the_rest(
    writer: LogWriter, database: Database, sink: LogSink
) -> None:
    await seed_settings(database, log_retention_days=30)
    emit(sink, "recent_line")
    await writer.flush_once()
    async with database.session() as session:
        row = (await SqlAlchemyLogRepository(session).search(limit=1))[0]
        row.ts = datetime.now(UTC) - timedelta(days=1)
    emit(sink, "ancient_line")
    await writer.flush_once()
    async with database.session() as session:
        rows = await SqlAlchemyLogRepository(session).search(limit=10)
        next(row for row in rows if row.message == "ancient_line").ts = datetime.now(
            UTC
        ) - timedelta(days=60)

    logs_removed, _ = await writer.prune_once()

    assert logs_removed == 1
    assert await stored_messages(database) == ["recent_line"]


async def test_audit_rows_outlive_application_logs(writer: LogWriter, database: Database) -> None:
    """Two ages on purpose: logs are diagnostic and expire in months, the trail is evidence
    and expires in years."""
    await seed_settings(database, log_retention_days=30, audit_retention_days=730)
    async with database.session() as session:
        old = await SqlAlchemyAuditRepository(session).record(
            action=AuditAction.LOGIN_SUCCESS, username="admin"
        )
        old.ts = datetime.now(UTC) - timedelta(days=100)

    _, audit_removed = await writer.prune_once()

    assert audit_removed == 0
    async with database.session() as session:
        assert len(await SqlAlchemyAuditRepository(session).recent()) == 1


async def test_an_audit_row_past_its_own_retention_is_removed(
    writer: LogWriter, database: Database
) -> None:
    await seed_settings(database, audit_retention_days=30)
    async with database.session() as session:
        old = await SqlAlchemyAuditRepository(session).record(
            action=AuditAction.LOGIN_FAILURE, username="admin"
        )
        old.ts = datetime.now(UTC) - timedelta(days=90)

    _, audit_removed = await writer.prune_once()

    assert audit_removed == 1
