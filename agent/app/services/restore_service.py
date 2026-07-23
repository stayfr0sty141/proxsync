"""Restore orchestration.

The agent enforces the guards that must hold on the host regardless of what the dashboard
believes: the archive exists and matches its recorded digest, the artifact's guest type
matches the endpoint, the target id is either free or explicitly marked for overwrite, and a
running target is only replaced when ``force_stop`` was requested.

The dashboard's two-phase confirmation sits *above* this; these checks are the floor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.concurrency import SlotBusyError, SlotManager
from app.core.config import AgentSettings
from app.core.errors import ConcurrencyConflict, GuestLocked, ValidationFailed
from app.core.logging import logger, set_correlation_id
from app.executors.base import ProcessHandle, ProcessRunner
from app.executors.checksum import compute_sha256
from app.executors.restore import (
    RestoreCommand,
    build_start_argv,
    build_status_argv,
    build_stop_argv,
    parse_status_output,
)
from app.schemas.enums import GuestType, TaskKind, TaskState
from app.schemas.requests import RestoreLxcRequest, RestoreVmRequest
from app.services.artifact_service import ArtifactService
from app.tasks.models import Task
from app.tasks.progress import RestoreProgressParser
from app.tasks.registry import TaskRegistry
from app.validators.identifiers import GuestLocator, StorageValidator

RESTORE_SLOT = "restore"


class RestoreService:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        registry: TaskRegistry,
        runner: ProcessRunner,
        guests: GuestLocator,
        storages: StorageValidator,
        artifacts: ArtifactService,
        slots: SlotManager,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._guests = guests
        self._storages = storages
        self._artifacts = artifacts
        self._slots = slots
        self._background: set[asyncio.Task[None]] = set()

    # ---- public API ----------------------------------------------------------

    async def restore_vm(self, request: RestoreVmRequest) -> Task:
        return await self._restore(request, guest_type=GuestType.VM)

    async def restore_lxc(self, request: RestoreLxcRequest) -> Task:
        return await self._restore(request, guest_type=GuestType.LXC)

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

    # ---- orchestration -------------------------------------------------------

    async def _restore(
        self, request: RestoreVmRequest | RestoreLxcRequest, *, guest_type: GuestType
    ) -> Task:
        artifact, archive_path = self._artifacts.resolve(request.archive)
        if artifact.guest_type is not guest_type:
            raise ValidationFailed(
                f"'{request.archive}' is a {artifact.guest_type.value} backup; "
                f"use the {artifact.guest_type.value} restore endpoint"
            )

        storage = await self._storages.require(request.storage)
        self._guests.validate_vmid(request.target_vmid)

        existing = self._guests.find(request.target_vmid)
        if existing is not None:
            if not request.overwrite:
                raise ValidationFailed(
                    f"VMID {request.target_vmid} already exists as a {existing.guest_type.value}. "
                    "Set overwrite to replace it."
                )
            if existing.guest_type is not guest_type:
                raise ValidationFailed(
                    f"VMID {request.target_vmid} is a {existing.guest_type.value}; "
                    f"it cannot be replaced by a {guest_type.value} restore"
                )
            await self._assert_stoppable(request, guest_type)

        if request.expected_sha256:
            await self._verify_digest(archive_path, request.expected_sha256)

        try:
            self._slots.try_acquire(RESTORE_SLOT)
        except SlotBusyError as exc:
            raise ConcurrencyConflict(
                "A restore is already running. Restores are serialised for safety."
            ) from exc

        kind = TaskKind.RESTORE_VM if guest_type is GuestType.VM else TaskKind.RESTORE_LXC
        try:
            task = self._registry.create(
                kind=kind,
                correlation_id=request.correlation_id,
                meta={
                    "archive": artifact.filename,
                    "source_vmid": artifact.vmid,
                    "target_vmid": request.target_vmid,
                    "guest_type": guest_type.value,
                    "storage": storage,
                    "overwrite": request.overwrite,
                },
            )
        except Exception:
            self._slots.release(RESTORE_SLOT)
            raise

        command = RestoreCommand(
            archive_path=archive_path,
            target_vmid=request.target_vmid,
            storage=storage,
            overwrite=request.overwrite,
            bwlimit_kbps=request.bwlimit_kbps or None,
            unprivileged=getattr(request, "unprivileged", None),
        )
        argv = (
            command.build_vm(self._settings.qmrestore_bin)
            if guest_type is GuestType.VM
            else command.build_lxc(self._settings.pct_bin)
        )

        runner_task = asyncio.create_task(
            self._execute(
                task,
                argv,
                guest_type=guest_type,
                target_vmid=request.target_vmid,
                stop_first=bool(existing and request.force_stop),
                start_after=request.start_after,
            )
        )
        self._background.add(runner_task)
        runner_task.add_done_callback(self._background.discard)
        return task

    async def _execute(
        self,
        task: Task,
        argv: list[str],
        *,
        guest_type: GuestType,
        target_vmid: int,
        stop_first: bool,
        start_after: bool,
    ) -> None:
        set_correlation_id(task.correlation_id)
        parser = RestoreProgressParser()
        handle = ProcessHandle()
        self._registry.attach_handle(task.id, handle)
        binary = self._guest_binary(guest_type)

        def on_line(line: str) -> None:
            if parser.feed(line, task.progress):
                self._registry.update_progress(task, task.progress)

        try:
            self._registry.mark_running(task)

            if stop_first:
                task.progress.message = "stopping target guest"
                self._registry.update_progress(task, task.progress, persist=True)
                stop_result = await self._runner.run_logged(
                    build_stop_argv(binary, target_vmid),
                    log_path=task.log_path,
                    timeout_seconds=self._settings.command_timeout_seconds * 5,
                )
                if not stop_result.ok:
                    self._registry.finish(
                        task,
                        state=TaskState.FAILED,
                        exit_code=stop_result.exit_code,
                        error=f"Could not stop guest {target_vmid} before restoring",
                    )
                    return

            task.progress.message = "restoring"
            self._registry.update_progress(task, task.progress, persist=True)
            result = await self._runner.run_logged(
                argv,
                log_path=task.log_path,
                timeout_seconds=self._settings.restore_timeout_seconds,
                on_line=on_line,
                handle=handle,
            )

            if result.cancelled:
                self._registry.finish(
                    task,
                    state=TaskState.CANCELLED,
                    exit_code=result.exit_code,
                    error="Cancelled by request. The target guest may be in a partial state.",
                )
                return

            if result.timed_out:
                self._registry.finish(
                    task,
                    state=TaskState.FAILED,
                    exit_code=result.exit_code,
                    error=f"Restore exceeded the {self._settings.restore_timeout_seconds}s timeout",
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

            outcome: dict[str, object] = {
                "target_vmid": target_vmid,
                "guest_type": guest_type.value,
                "duration_seconds": round(result.duration_seconds, 2),
                "started": False,
            }

            if start_after:
                start_result = await self._runner.run_logged(
                    build_start_argv(binary, target_vmid),
                    log_path=task.log_path,
                    timeout_seconds=self._settings.command_timeout_seconds * 5,
                )
                outcome["started"] = start_result.ok
                if not start_result.ok:
                    # The restore itself succeeded; failing to start is reported, not fatal.
                    outcome["warnings"] = [
                        f"Restore completed but guest {target_vmid} failed to start "
                        f"(exit {start_result.exit_code})"
                    ]

            self._registry.finish(task, state=TaskState.SUCCESS, exit_code=0, result=outcome)

        except Exception as exc:  # noqa: BLE001 - always reach a terminal state
            logger.error("restore_task_crashed", task_id=task.id, exc_info=exc)
            self._registry.finish(
                task, state=TaskState.FAILED, error=f"Agent error while restoring: {exc}"
            )
        finally:
            self._slots.release(RESTORE_SLOT)

    # ---- guards --------------------------------------------------------------

    async def _assert_stoppable(
        self, request: RestoreVmRequest | RestoreLxcRequest, guest_type: GuestType
    ) -> None:
        status = await self._guest_status(guest_type, request.target_vmid)
        if status == "running" and not request.force_stop:
            raise GuestLocked(
                f"Guest {request.target_vmid} is running. "
                "Set force_stop to shut it down before restoring."
            )

    async def _guest_status(self, guest_type: GuestType, vmid: int) -> str:
        binary = self._guest_binary(guest_type)
        exit_code, output = await self._runner.run_capture(
            build_status_argv(binary, guest_type, vmid),
            timeout_seconds=self._settings.command_timeout_seconds,
        )
        if exit_code != 0:
            return "unknown"
        return parse_status_output(output)

    def _guest_binary(self, guest_type: GuestType) -> Path:
        return self._settings.qm_bin if guest_type is GuestType.VM else self._settings.pct_bin

    async def _verify_digest(self, archive_path: Path, expected: str) -> None:
        checksum = await compute_sha256(
            archive_path, chunk_bytes=self._settings.checksum_chunk_bytes
        )
        if checksum.hex_digest.lower() != expected.lower():
            raise ValidationFailed(
                "Archive digest does not match the expected value; refusing to restore. "
                f"expected={expected[:16]}… actual={checksum.hex_digest[:16]}…"
            )


def _tail_summary(lines: tuple[str, ...], limit: int = 3) -> str:
    meaningful = [line for line in lines if line.strip()][-limit:]
    return " | ".join(meaningful) if meaningful else "Command failed without output"
