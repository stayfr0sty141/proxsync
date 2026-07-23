"""Drains the log sink into the database, and prunes what has aged out.

Separate from the sink itself because the two have opposite constraints: capture must be
synchronous, allocation-light and infallible, while persistence is a batched async write that
is allowed to fail. Keeping them apart is what lets a database outage cost log lines instead
of costing backups.

A failed batch is put back at the front of the queue rather than discarded, so a five-second
database hiccup delays log rows instead of losing them. What it cannot survive is the buffer
filling in the meantime — at that point the oldest entries go, by design, and the drop counter
in `/health/detail` says so.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.log_sink import CapturedLog, LogSink, log_sink
from app.core.logging import logger, set_correlation_id
from app.db.session import Database
from app.repositories.audit_repository import SqlAlchemyAuditRepository
from app.repositories.log_repository import NewLogEntry, SqlAlchemyLogRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import SettingsSection
from app.schemas.settings import GeneralSettings
from app.services.settings_service import SettingsService


class LogWriter:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        secret_box: SecretBox,
        sink: LogSink | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._secret_box = secret_box
        self._sink = sink or log_sink

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_error: str | None = None
        self._last_prune_at: float | None = None
        self._written = 0

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="proxsync-log-writer")
        logger.info("log_writer_started", persist_level=self._settings.log_persist_level)

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=10)
        # Everything logged during shutdown is still in the buffer; this is the last chance to
        # persist the lines explaining why the process is stopping.
        with contextlib.suppress(Exception):
            await self.flush_once()
        logger.info("log_writer_stopped", written=self._written)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def written(self) -> int:
        return self._written

    @property
    def dropped(self) -> int:
        return self._sink.dropped

    @property
    def pending(self) -> int:
        return self._sink.pending

    # ---- loop ----------------------------------------------------------------

    async def _loop(self) -> None:
        await self._maybe_prune(force=True)
        while not self._stopping.is_set():
            try:
                await self.flush_once()
                await self._maybe_prune()
            except Exception as exc:  # noqa: BLE001 - one bad batch must not stop persistence
                self._last_error = str(exc)
                logger.error("log_flush_failed", error=str(exc), exc_info=True)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.log_flush_interval_seconds
                )

    async def flush_once(self) -> int:
        """Persist one batch. Returns how many rows were written."""
        entries = self._sink.drain(self._settings.log_flush_batch_size)
        if not entries:
            return 0

        # Capture stays off for the whole write: SQLAlchemy failures surface through this
        # worker's own logger, and re-capturing them would refill the queue being drained.
        unlinked = False
        with self._sink.paused():
            try:
                await self._insert([_to_entry(entry) for entry in entries])
            except IntegrityError:
                # A line named a row that is not committed yet, or has since been deleted. The
                # line is the point, not the join — it is written with the links dropped rather
                # than retried forever, which would wedge every log row behind one bad batch.
                # The ids remain readable in `context`.
                await self._insert([_unlinked(_to_entry(entry)) for entry in entries])
                unlinked = True
            except Exception as exc:
                self._sink.restore(entries)
                self._last_error = str(exc)
                raise

        if unlinked:
            logger.warning("log_entries_unlinked", count=len(entries))
        self._last_error = None
        self._written += len(entries)
        return len(entries)

    async def _insert(self, entries: list[NewLogEntry]) -> None:
        async with self._database.session() as session:
            await SqlAlchemyLogRepository(session).add_many(entries)

    # ---- retention -----------------------------------------------------------

    async def _maybe_prune(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        interval = self._settings.log_prune_interval_seconds
        if not force and self._last_prune_at is not None and now - self._last_prune_at < interval:
            return
        self._last_prune_at = now
        await self.prune_once()

    async def prune_once(self) -> tuple[int, int]:
        """Delete aged-out log and audit rows. Returns `(logs, audit_events)` removed.

        The two ages are deliberately different settings: application logs are diagnostic and
        expire in months, the audit trail is evidence and expires in years.
        """
        set_correlation_id(None)
        general = await self._general_settings()
        now = datetime.now(UTC)

        with self._sink.paused():
            async with self._database.session() as session:
                logs = await SqlAlchemyLogRepository(session).prune(
                    before=now - timedelta(days=general.log_retention_days)
                )
                audit = await SqlAlchemyAuditRepository(session).prune(
                    before=now - timedelta(days=general.audit_retention_days)
                )

        if logs or audit:
            logger.info(
                "log_retention_applied",
                logs_removed=logs,
                audit_removed=audit,
                log_retention_days=general.log_retention_days,
                audit_retention_days=general.audit_retention_days,
            )
        return logs, audit

    async def _general_settings(self) -> GeneralSettings:
        async with self._database.session() as session:
            section = await SettingsService(
                repository=SqlAlchemySettingsRepository(session), secret_box=self._secret_box
            ).get_section(SettingsSection.GENERAL)
        assert isinstance(section, GeneralSettings)
        return section


def _unlinked(entry: NewLogEntry) -> NewLogEntry:
    """The same line with every foreign key cleared."""
    return replace(entry, user_id=None, backup_id=None, restore_id=None, sync_task_id=None)


def _to_entry(captured: CapturedLog) -> NewLogEntry:
    return NewLogEntry(
        ts=captured.ts,
        level=captured.level,
        category=captured.category,
        message=captured.message,
        context=captured.context,
        correlation_id=captured.correlation_id,
        user_id=captured.user_id,
        backup_id=captured.backup_id,
        restore_id=captured.restore_id,
        sync_task_id=captured.sync_task_id,
    )
