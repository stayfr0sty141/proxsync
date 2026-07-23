"""Google Drive replication through rclone.

Transfers run as tracked tasks, exactly like backups: validated synchronously so the caller
learns immediately whether the request was accepted, then executed in the background with
progress streamed to the task journal.

**Uploads never overwrite a local file, and downloads refuse to by default.** A download that
silently replaced a local artifact could destroy the only good copy of a backup — the very
thing this application exists to prevent — so it must be asked for explicitly.

**Verification compares what the remote actually holds.** Google Drive stores an MD5 for
every uploaded file and returns it without a download; asking for SHA-256 would make rclone
fetch the whole artifact back. When a remote publishes no hash at all, that is reported as
`hash_unavailable` rather than counted as verified: a truncated file of exactly the right
length would otherwise pass a size-only check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.concurrency import SlotBusyError, SlotManager
from app.core.config import AgentSettings
from app.core.errors import (
    ConcurrencyConflict,
    ExecutionFailed,
    NotFound,
    ValidationFailed,
)
from app.core.logging import logger, set_correlation_id
from app.executors.base import ProcessHandle, ProcessRunner
from app.executors.checksum import compute_md5
from app.executors.rclone import (
    RcloneOptions,
    RcloneProgressParser,
    RemoteEntry,
    RemoteQuota,
    build_about_argv,
    build_copy_argv,
    build_delete_argv,
    build_lsjson_argv,
    parse_about,
    parse_lsjson,
)
from app.schemas.enums import TaskKind, TaskState, VerifyOutcome
from app.schemas.requests import (
    SyncDeleteRequest,
    SyncDownloadRequest,
    SyncUploadRequest,
    SyncVerifyRequest,
)
from app.schemas.responses import VerifyResultResponse
from app.services.artifact_service import ArtifactService
from app.tasks.models import Task
from app.tasks.registry import TaskRegistry
from app.validators.artifacts import parse_artifact_name
from app.validators.paths import resolve_within, validate_basename
from app.validators.remotes import (
    build_remote_spec,
    validate_remote_name,
    validate_remote_path,
)

SYNC_SLOT = "sync"


class SyncService:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        registry: TaskRegistry,
        runner: ProcessRunner,
        artifacts: ArtifactService,
        slots: SlotManager,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._artifacts = artifacts
        self._slots = slots
        self._background: set[asyncio.Task[None]] = set()

    # ---- helpers -------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._settings.sync_enabled:
            raise ValidationFailed(
                "Sync is disabled on this agent (PROXSYNC_AGENT_SYNC_ENABLED=false)"
            )

    def _options(self, *, bwlimit_kbps: int = 0, transfers: int | None = None) -> RcloneOptions:
        return RcloneOptions(
            rclone_bin=self._settings.rclone_bin,
            config_path=self._settings.rclone_config,
            transfers=transfers or self._settings.rclone_transfers,
            checkers=self._settings.rclone_checkers,
            bwlimit_kbps=bwlimit_kbps,
            low_level_retries=self._settings.rclone_low_level_retries,
            stats_interval_seconds=self._settings.rclone_stats_interval_seconds,
        )

    def _validated_remote(self, remote: str, remote_path: str) -> tuple[str, str]:
        name = validate_remote_name(remote, allowed=self._settings.allowed_remotes)
        path = validate_remote_path(remote_path)
        return name, path

    def _local_artifact(self, filename: str) -> Path:
        """Resolve a local artifact, proving it is a real vzdump file inside the dump root."""
        parse_artifact_name(filename)
        return resolve_within(self._artifacts.dump_root, filename)

    def _acquire(self) -> None:
        try:
            self._slots.try_acquire(SYNC_SLOT)
        except SlotBusyError as exc:
            raise ConcurrencyConflict(
                f"A transfer is already running (capacity {exc.capacity}). Retry when it finishes."
            ) from exc

    # ---- transfers -----------------------------------------------------------

    def upload(self, request: SyncUploadRequest) -> Task:
        self._require_enabled()
        remote, remote_path = self._validated_remote(request.remote, request.remote_path)
        source = self._local_artifact(request.filename)
        if not source.is_file():
            raise NotFound(f"Backup '{request.filename}' does not exist on this host")

        destination = build_remote_spec(remote, remote_path, request.filename)
        argv = build_copy_argv(
            self._options(bwlimit_kbps=request.bwlimit_kbps, transfers=request.transfers),
            source=str(source),
            destination=destination,
        )

        return self._schedule(
            kind=TaskKind.UPLOAD,
            argv=argv,
            correlation_id=request.correlation_id,
            meta={
                "filename": request.filename,
                "remote": remote,
                "remote_path": remote_path,
                "size_bytes": source.stat().st_size,
            },
            verify=(
                SyncVerifyRequest(filename=request.filename, remote=remote, remote_path=remote_path)
                if request.verify_after
                else None
            ),
        )

    def download(self, request: SyncDownloadRequest) -> Task:
        self._require_enabled()
        remote, remote_path = self._validated_remote(request.remote, request.remote_path)

        # The name is validated as an artifact even on the way *in*: a remote that has been
        # tampered with must not be able to write `../../etc/cron.d/anything` onto the host.
        parse_artifact_name(request.filename)
        destination = resolve_within(self._artifacts.dump_root, request.filename)

        if destination.exists() and not request.overwrite:
            raise ValidationFailed(
                f"'{request.filename}' already exists locally. Pass overwrite=true only if "
                "you are sure the local copy should be replaced."
            )

        source = build_remote_spec(remote, remote_path, request.filename)
        argv = build_copy_argv(
            self._options(bwlimit_kbps=request.bwlimit_kbps, transfers=request.transfers),
            source=source,
            destination=str(destination),
        )

        return self._schedule(
            kind=TaskKind.DOWNLOAD,
            argv=argv,
            correlation_id=request.correlation_id,
            meta={
                "filename": request.filename,
                "remote": remote,
                "remote_path": remote_path,
            },
        )

    def _schedule(
        self,
        *,
        kind: TaskKind,
        argv: list[str],
        correlation_id: str | None,
        meta: dict[str, object],
        verify: SyncVerifyRequest | None = None,
    ) -> Task:
        self._acquire()
        try:
            task = self._registry.create(kind=kind, correlation_id=correlation_id, meta=meta)
        except Exception:
            self._slots.release(SYNC_SLOT)
            raise

        runner_task = asyncio.create_task(self._execute(task, argv, verify=verify))
        self._background.add(runner_task)
        runner_task.add_done_callback(self._background.discard)
        return task

    async def _execute(
        self, task: Task, argv: list[str], *, verify: SyncVerifyRequest | None
    ) -> None:
        set_correlation_id(task.correlation_id)
        parser = RcloneProgressParser()
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
                timeout_seconds=self._settings.sync_timeout_seconds,
                on_line=on_line,
                handle=handle,
            )

            if result.cancelled:
                self._registry.finish(task, state=TaskState.CANCELLED, exit_code=result.exit_code)
                return
            if result.timed_out:
                self._registry.finish(
                    task,
                    state=TaskState.FAILED,
                    exit_code=result.exit_code,
                    error=f"Transfer timed out after {self._settings.sync_timeout_seconds}s",
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
                "duration_seconds": round(result.duration_seconds, 2),
                "bytes_transferred": task.progress.bytes_done,
            }

            if verify is not None:
                # A transfer that rclone called successful but whose result does not match
                # the source is a failed transfer, not a successful one with a footnote.
                verification = await self.verify(verify)
                outcome["verification"] = verification.model_dump(mode="json")
                if not verification.verified:
                    self._registry.finish(
                        task,
                        state=TaskState.FAILED,
                        exit_code=0,
                        error=(
                            f"Upload completed but verification reported "
                            f"'{verification.outcome.value}': {verification.detail}"
                        ),
                        result=outcome,
                    )
                    return

            self._registry.finish(task, state=TaskState.SUCCESS, exit_code=0, result=outcome)

        except Exception as exc:  # noqa: BLE001 - the task must always reach a terminal state
            logger.error("sync_task_crashed", task_id=task.id, exc_info=exc)
            self._registry.finish(
                task, state=TaskState.FAILED, error=f"Agent error during transfer: {exc}"
            )
        finally:
            self._slots.release(SYNC_SLOT)

    # ---- queries -------------------------------------------------------------

    async def list_remote(self, *, remote: str, remote_path: str) -> list[RemoteEntry]:
        self._require_enabled()
        name, path = self._validated_remote(remote, remote_path)
        argv = build_lsjson_argv(self._options(), target=build_remote_spec(name, path))

        exit_code, output = await self._runner.run_capture(
            argv, timeout_seconds=self._settings.command_timeout_seconds
        )
        if exit_code != 0:
            if "directory not found" in output.lower():
                return []
            raise ExecutionFailed(f"rclone lsjson exited {exit_code}: {output.strip()[:300]}")
        return parse_lsjson(output)

    async def quota(self, *, remote: str) -> RemoteQuota:
        self._require_enabled()
        name = validate_remote_name(remote, allowed=self._settings.allowed_remotes)
        argv = build_about_argv(self._options(), remote_spec=f"{name}:")

        exit_code, output = await self._runner.run_capture(
            argv, timeout_seconds=self._settings.command_timeout_seconds
        )
        if exit_code != 0:
            raise ExecutionFailed(f"rclone about exited {exit_code}: {output.strip()[:300]}")
        return parse_about(output)

    async def verify(self, request: SyncVerifyRequest) -> VerifyResultResponse:
        """Compare a local artifact with its remote copy."""
        self._require_enabled()
        remote, remote_path = self._validated_remote(request.remote, request.remote_path)
        source = self._local_artifact(request.filename)
        if not source.is_file():
            raise NotFound(f"Backup '{request.filename}' does not exist on this host")

        local_size = source.stat().st_size
        entries = await self.list_remote(remote=remote, remote_path=remote_path)
        match = next(
            (entry for entry in entries if entry.name == request.filename and not entry.is_dir),
            None,
        )

        base = {
            "filename": request.filename,
            "remote": remote,
            "remote_path": remote_path,
            "local_size_bytes": local_size,
        }

        if match is None:
            return VerifyResultResponse(
                **base,
                outcome=VerifyOutcome.MISSING_REMOTE,
                verified=False,
                detail=f"'{request.filename}' is not present at {remote}:{remote_path}",
            )

        if match.size_bytes != local_size:
            return VerifyResultResponse(
                **base,
                outcome=VerifyOutcome.SIZE_MISMATCH,
                verified=False,
                remote_size_bytes=match.size_bytes,
                detail=(f"Local file is {local_size} bytes, remote copy is {match.size_bytes}"),
            )

        if match.md5 is None:
            return VerifyResultResponse(
                **base,
                outcome=VerifyOutcome.HASH_UNAVAILABLE,
                verified=False,
                remote_size_bytes=match.size_bytes,
                detail=(
                    f"Sizes agree, but remote '{remote}' publishes no MD5 to compare against. "
                    "The copy is the right length; its contents are unconfirmed."
                ),
            )

        local_md5 = (await compute_md5(source)).hex_digest
        if local_md5.lower() != match.md5.lower():
            return VerifyResultResponse(
                **base,
                outcome=VerifyOutcome.HASH_MISMATCH,
                verified=False,
                remote_size_bytes=match.size_bytes,
                local_md5=local_md5,
                remote_md5=match.md5,
                detail="Sizes agree but the MD5 digests differ; the remote copy is corrupt",
            )

        logger.info("sync_verified", filename=request.filename, remote=remote)
        return VerifyResultResponse(
            **base,
            outcome=VerifyOutcome.MATCH,
            verified=True,
            remote_size_bytes=match.size_bytes,
            local_md5=local_md5,
            remote_md5=match.md5,
        )

    async def delete_remote(self, request: SyncDeleteRequest) -> str:
        """Remove one remote file. Returns the spec that was deleted."""
        self._require_enabled()
        remote, remote_path = self._validated_remote(request.remote, request.remote_path)
        validate_basename(request.filename)
        parse_artifact_name(request.filename)

        target = build_remote_spec(remote, remote_path, request.filename)
        argv = build_delete_argv(self._options(), target=target)

        exit_code, output = await self._runner.run_capture(
            argv, timeout_seconds=self._settings.command_timeout_seconds
        )
        if exit_code != 0:
            lowered = output.lower()
            if "object not found" in lowered or "not found" in lowered:
                raise NotFound(f"'{request.filename}' is not present at {remote}:{remote_path}")
            raise ExecutionFailed(f"rclone deletefile exited {exit_code}: {output.strip()[:300]}")

        logger.info("sync_remote_deleted", filename=request.filename, remote=remote)
        return target

    async def cancel(self, task_id: str) -> bool:
        handle = self._registry.handle(task_id)
        if handle is None:
            return False
        return await handle.cancel(grace_seconds=self._settings.cancel_grace_seconds)

    @property
    def in_flight(self) -> int:
        return len(self._background)

    async def drain(self) -> None:
        if not self._background:
            return
        await asyncio.wait(set(self._background))


def _tail_summary(lines: tuple[str, ...]) -> str:
    tail = " | ".join(line.strip() for line in lines[-5:] if line.strip())
    return tail[:500] or "rclone failed without producing output"
