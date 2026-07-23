"""Process execution.

The single place in the agent where a child process is created. Rules enforced here rather
than trusted to callers:

* ``asyncio.create_subprocess_exec`` only — there is no shell, so no quoting, globbing,
  redirection or command chaining is possible.
* argv must be a list of plain strings; the executable must be an existing absolute path.
* stdout and stderr are merged and streamed line-by-line to a per-task log file, so a
  40-minute vzdump never buffers in memory.
* A timeout terminates the process group: SIGTERM, then SIGKILL after a grace period.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ExecutionFailed, ValidationFailed
from app.core.logging import logger

LineHandler = Callable[[str], None]

# Environment handed to children: no inherited PATH manipulation, no user locale surprises.
_CHILD_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    last_lines: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


class ProcessHandle:
    """Lets a service cancel a running child without reaching into the executor."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._cancelled = False

    def attach(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def cancel(self, *, grace_seconds: int) -> bool:
        """Terminate the child. Returns False when there is nothing to cancel."""
        process = self._process
        if process is None or process.returncode is not None:
            return False
        self._cancelled = True
        _signal_group(process.pid, signal.SIGTERM)
        try:
            async with asyncio.timeout(grace_seconds):
                await process.wait()
        except TimeoutError:
            _signal_group(process.pid, signal.SIGKILL)
        return True


def _signal_group(pid: int | None, sig: signal.Signals) -> None:
    """Signal the child's whole process group — vzdump spawns helpers that must die with it."""
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
        agent_pgid = os.getpgid(0)
        # Never signal our own process group via killpg
        if pgid == agent_pgid:
            os.kill(pid, sig)
        else:
            os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):  # pragma: no cover - defensive fallback
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


def validate_argv(argv: Sequence[str]) -> list[str]:
    if not argv:
        raise ValidationFailed("Empty command")
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            raise ValidationFailed(f"Command argument {index} is not a string")
        if "\x00" in item:
            raise ValidationFailed(f"Command argument {index} contains a null byte")
    executable = Path(argv[0])
    if not executable.is_absolute():
        raise ValidationFailed(f"Executable must be an absolute path: {argv[0]}")
    return list(argv)


class ProcessRunner:
    def __init__(self, *, cancel_grace_seconds: int = 30, tail_lines: int = 40) -> None:
        self._grace = cancel_grace_seconds
        self._tail_lines = tail_lines

    async def run_logged(
        self,
        argv: Sequence[str],
        *,
        log_path: Path,
        timeout_seconds: int,
        on_line: LineHandler | None = None,
        handle: ProcessHandle | None = None,
    ) -> ProcessResult:
        """Run a long command, streaming merged output to ``log_path``."""
        command = validate_argv(argv)
        started = time.monotonic()
        tail: list[str] = []
        timed_out = False

        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("process_start", executable=command[0], argc=len(command), log=str(log_path))

        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"$ {' '.join(command)}\n")
            log_file.flush()

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env=_CHILD_ENV,
                start_new_session=True,  # own process group, so cancellation reaches helpers
            )
            if handle is not None:
                handle.attach(process)

            assert process.stdout is not None

            async def pump() -> None:
                async for raw in process.stdout:  # type: ignore[union-attr]
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    log_file.write(line + "\n")
                    log_file.flush()
                    tail.append(line)
                    if len(tail) > self._tail_lines:
                        del tail[0]
                    if on_line is not None:
                        try:
                            on_line(line)
                        except Exception:  # noqa: BLE001 - a parser bug must not kill the task
                            logger.warning("line_handler_failed", line=line, exc_info=True)

            try:
                async with asyncio.timeout(timeout_seconds):
                    await pump()
                    await process.wait()
            except TimeoutError:
                timed_out = True
                logger.error("process_timeout", executable=command[0], timeout=timeout_seconds)
                _signal_group(process.pid, signal.SIGTERM)
                try:
                    async with asyncio.timeout(self._grace):
                        await process.wait()
                except TimeoutError:
                    _signal_group(process.pid, signal.SIGKILL)
                    await process.wait()
                log_file.write(f"\n[proxsync] terminated after {timeout_seconds}s timeout\n")

        exit_code = process.returncode if process.returncode is not None else -1
        result = ProcessResult(
            exit_code=exit_code,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            cancelled=handle.cancelled if handle else False,
            last_lines=tuple(tail),
        )
        logger.info(
            "process_finished",
            executable=command[0],
            exit_code=result.exit_code,
            duration=round(result.duration_seconds, 2),
            timed_out=timed_out,
        )
        return result

    async def run_capture(self, argv: Sequence[str], *, timeout_seconds: int) -> tuple[int, str]:
        """Run a short command and capture its output. Used for status queries only."""
        command = validate_argv(argv)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=_CHILD_ENV,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, _ = await process.communicate()
        except TimeoutError:
            _signal_group(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
            raise ExecutionFailed(f"'{command[0]}' timed out after {timeout_seconds}s") from None
        except Exception:
            _signal_group(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
            raise

        return process.returncode or 0, stdout.decode("utf-8", errors="replace")
