"""Backup orchestration.

Validation happens synchronously so the caller learns immediately whether the request was
accepted; execution happens in a background task the registry tracks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.concurrency import SlotBusyError, SlotManager
from app.core.config import AgentSettings
from app.core.errors import ConcurrencyConflict
from app.core.logging import logger, set_correlation_id
from app.executors.base import ProcessHandle, ProcessRunner
from app.executors.checksum import compute_sha256
from app.executors.vzdump import VzdumpCommand
from app.schemas.enums import TaskKind, TaskState
from app.schemas.requests import BackupStartRequest
from app.tasks.models import Task
from app.tasks.progress import VzdumpProgressParser
from app.tasks.registry import TaskRegistry
from app.validators.artifacts import is_artifact_name
from app.validators.identifiers import GuestLocator, StorageValidator
from app.validators.paths import assert_within

BACKUP_SLOT = "backup"


class BackupService:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        registry: TaskRegistry,
        runner: ProcessRunner,
        guests: GuestLocator,
        storages: StorageValidator,
        slots: SlotManager,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._guests = guests
        self._storages = storages
        self._slots = slots
        self._background: set[asyncio.Task[None]] = set()

    async def start(self, request: BackupStartRequest) -> Task:
        """Validate, reserve a slot, and schedule the vzdump run."""
        guest = self._guests.require(request.vmid, request.guest_type)
        storage = await self._storages.require(request.storage)

        try:
            self._slots.try_acquire(BACKUP_SLOT)
        except SlotBusyError as exc:
            raise ConcurrencyConflict(
                f"A backup is already running (capacity {exc.capacity}). Retry when it finishes."
            ) from exc

        try:
            task = self._registry.create(
                kind=TaskKind.BACKUP,
                correlation_id=request.correlation_id,
                meta={
                    "vmid": guest.vmid,
                    "guest_type": guest.guest_type.value,
                    "mode": request.mode.value,
                    "compression": request.compression.value,
                    "storage": storage,
                },
            )
        except Exception:
            self._slots.release(BACKUP_SLOT)
            raise

        command = VzdumpCommand(
            vmid=guest.vmid,
            mode=request.mode,
            compression=request.compression,
            storage=storage,
            zstd_threads=request.zstd_threads,
            bwlimit_kbps=request.bwlimit_kbps or None,
            tmpdir=self._settings.temp_dir if self._settings.temp_dir else None,
        )
        argv = command.build(self._settings.vzdump_bin)

        runner_task = asyncio.create_task(self._execute(task, argv, notes=request.notes))
        self._background.add(runner_task)
        runner_task.add_done_callback(self._background.discard)
        return task

    async def _execute(self, task: Task, argv: list[str], *, notes: str | None) -> None:
        set_correlation_id(task.correlation_id)
        parser = VzdumpProgressParser()
        handle = ProcessHandle()
        self._registry.attach_handle(task.id, handle)

        def on_line(line: str) -> None:
            if task.pid is None and handle.pid is not None:
                task.pid = handle.pid
            if parser.feed(line, task.progress):
                self._registry.update_progress(task, task.progress)

        try:
            self._registry.mark_running(task)
            result = await self._runner.run_logged(
                argv,
                log_path=task.log_path,
                timeout_seconds=self._settings.backup_timeout_seconds,
                on_line=on_line,
                handle=handle,
            )

            if result.cancelled:
                self._registry.finish(
                    task,
                    state=TaskState.CANCELLED,
                    exit_code=result.exit_code,
                    error="Cancelled by request",
                )
                return

            if result.timed_out:
                self._registry.finish(
                    task,
                    state=TaskState.FAILED,
                    exit_code=result.exit_code,
                    error=f"vzdump exceeded the {self._settings.backup_timeout_seconds}s timeout",
                )
                return

            if not result.ok:
                self._registry.finish(
                    task,
                    state=TaskState.FAILED,
                    exit_code=result.exit_code,
                    error=parser.error_message or _tail_summary(result.last_lines),
                )
                return

            outcome = await self._collect_artifact(parser, notes=notes)
            outcome["duration_seconds"] = round(result.duration_seconds, 2)
            self._registry.finish(task, state=TaskState.SUCCESS, exit_code=0, result=outcome)

        except Exception as exc:  # noqa: BLE001 - the task must always reach a terminal state
            logger.error("backup_task_crashed", task_id=task.id, exc_info=exc)
            self._registry.finish(
                task, state=TaskState.FAILED, error=f"Agent error while running backup: {exc}"
            )
        finally:
            self._slots.release(BACKUP_SLOT)

    async def _collect_artifact(
        self, parser: VzdumpProgressParser, *, notes: str | None
    ) -> dict[str, object]:
        """Turn a successful run into the metadata the dashboard records."""
        outcome: dict[str, object] = {}
        warnings: list[str] = []

        if parser.archive_path is None:
            warnings.append("vzdump reported success but no archive path was found in its output")
            outcome["warnings"] = warnings
            return outcome

        archive = Path(parser.archive_path)
        outcome["filename"] = archive.name
        outcome["path"] = str(archive)

        if not is_artifact_name(archive.name):
            warnings.append(f"'{archive.name}' does not match the expected vzdump naming pattern")

        try:
            contained = assert_within(self._settings.dump_root, archive)
        except Exception:
            warnings.append(
                f"Archive was written outside {self._settings.dump_root}; "
                "checksum and notes were skipped"
            )
            outcome["warnings"] = warnings
            return outcome

        try:
            outcome["size_bytes"] = contained.stat().st_size
        except OSError:
            warnings.append("Archive disappeared before its size could be read")

        if notes:
            _write_notes(contained, notes, warnings)

        if self._settings.checksum_after_backup:
            try:
                checksum = await compute_sha256(
                    contained,
                    chunk_bytes=self._settings.checksum_chunk_bytes,
                    use_cache=False,
                )
                outcome["checksum_sha256"] = checksum.hex_digest
            except OSError as exc:
                warnings.append(f"Checksum could not be computed: {exc}")

        if parser.archive_size_bytes is not None:
            outcome["reported_size_bytes"] = parser.archive_size_bytes
        if warnings:
            outcome["warnings"] = warnings
        return outcome

    async def cancel(self, task_id: str) -> bool:
        handle = self._registry.handle(task_id)
        if handle is None:
            return False
        return await handle.cancel(grace_seconds=self._settings.cancel_grace_seconds)

    @property
    def in_flight(self) -> int:
        return len(self._background)

    async def drain(self) -> None:
        """Await in-flight executions. Used at shutdown and by the test suite."""
        if self._background:
            await asyncio.wait(set(self._background))


def _write_notes(archive: Path, notes: str, warnings: list[str]) -> None:
    """Write the PVE-compatible ``<archive>.notes`` sidecar.

    ProxSync writes this itself rather than passing ``--notes-template`` to vzdump, whose
    spelling and template semantics differ across PVE releases.
    """
    try:
        archive.with_name(archive.name + ".notes").write_text(notes + "\n", encoding="utf-8")
    except OSError as exc:
        warnings.append(f"Notes sidecar could not be written: {exc}")


def _tail_summary(lines: tuple[str, ...], limit: int = 3) -> str:
    meaningful = [line for line in lines if line.strip()][-limit:]
    return " | ".join(meaningful) if meaningful else "Command failed without output"
