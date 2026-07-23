"""Regression tests for process group isolation and descendant process cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

import pytest

from app.core.errors import ExecutionFailed
from app.executors.base import ProcessHandle, ProcessResult, ProcessRunner, _signal_group


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    # Check Linux /proc/<pid>/stat if present for zombie state 'Z'
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            content = proc_stat.read_text(encoding="utf-8")
            parts = content.split()
            if len(parts) >= 3 and parts[2] == "Z":
                return False
        except OSError:
            pass
    return True


@pytest.mark.asyncio
async def test_process_timeout_kills_child_and_grandchild(tmp_path: Path) -> None:
    runner = ProcessRunner(cancel_grace_seconds=1)
    log_path = tmp_path / "test.log"
    pid_file = tmp_path / "grandchild.pid"

    # Child script that spawns a grandchild and prints grandchild's PID
    child_script = tmp_path / "child.sh"
    child_script.write_text(
        f"""#!/usr/bin/env bash
sleep 60 &
GC_PID=$!
echo "$GC_PID" > "{pid_file}"
wait $GC_PID
""",
        encoding="utf-8",
    )
    child_script.chmod(0o755)

    result = await runner.run_logged([str(child_script)], log_path=log_path, timeout_seconds=1)

    assert result.timed_out is True

    # Give OS a moment to reap processes
    await asyncio.sleep(0.2)

    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())

    assert not _is_process_alive(grandchild_pid)


@pytest.mark.asyncio
async def test_coroutine_cancellation_kills_process_group(tmp_path: Path) -> None:
    runner = ProcessRunner(cancel_grace_seconds=1)
    log_path = tmp_path / "cancel.log"
    pid_file = tmp_path / "grandchild_cancel.pid"

    child_script = tmp_path / "child_cancel.sh"
    child_script.write_text(
        f"""#!/usr/bin/env bash
sleep 60 &
GC_PID=$!
echo "$GC_PID" > "{pid_file}"
wait $GC_PID
""",
        encoding="utf-8",
    )
    child_script.chmod(0o755)

    task = asyncio.create_task(
        runner.run_logged([str(child_script)], log_path=log_path, timeout_seconds=30)
    )

    # Wait for grandchild PID file to be written
    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.1)

    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _is_process_alive(grandchild_pid)

    # Cancel coroutine
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.2)
    assert not _is_process_alive(grandchild_pid)


@pytest.mark.asyncio
async def test_run_capture_timeout_kills_process_group(tmp_path: Path) -> None:
    runner = ProcessRunner()
    pid_file = tmp_path / "capture_gc.pid"

    child_script = tmp_path / "child_capture.sh"
    child_script.write_text(
        f"""#!/usr/bin/env bash
sleep 60 &
GC_PID=$!
echo "$GC_PID" > "{pid_file}"
wait $GC_PID
""",
        encoding="utf-8",
    )
    child_script.chmod(0o755)

    task_coro = runner.run_capture([str(child_script)], timeout_seconds=1)
    with pytest.raises(ExecutionFailed, match="timed out"):
        await task_coro

    await asyncio.sleep(0.2)
    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert not _is_process_alive(grandchild_pid)


@pytest.mark.asyncio
async def test_process_ignoring_sigterm_falls_back_to_sigkill(tmp_path: Path) -> None:
    runner = ProcessRunner(cancel_grace_seconds=1)
    log_path = tmp_path / "sigkill.log"

    stubborn_script = tmp_path / "stubborn.sh"
    stubborn_script.write_text(
        """#!/usr/bin/env bash
trap '' TERM
sleep 60
""",
        encoding="utf-8",
    )
    stubborn_script.chmod(0o755)

    start = time.monotonic()
    result = await runner.run_logged([str(stubborn_script)], log_path=log_path, timeout_seconds=1)
    duration = time.monotonic() - start

    assert result.timed_out is True
    # Should take timeout_seconds (1s) + grace_seconds (~1s) before SIGKILL
    assert duration >= 1.0


def test_signal_group_handles_nonexistent_pid() -> None:
    # Must not raise exception when signaling invalid PID
    _signal_group(99999999, signal.SIGTERM)


@pytest.mark.asyncio
async def test_process_handle_cancellation(tmp_path: Path) -> None:
    runner = ProcessRunner(cancel_grace_seconds=1)
    log_path = tmp_path / "handle.log"
    handle = ProcessHandle()

    task = asyncio.create_task(
        runner.run_logged(
            ["/bin/sleep", "30"], log_path=log_path, timeout_seconds=30, handle=handle
        )
    )

    await asyncio.sleep(0.2)
    assert handle.pid is not None

    cancelled = await handle.cancel(grace_seconds=1)
    assert cancelled is True
    result = await task
    assert result.cancelled is True


@pytest.mark.asyncio
async def test_process_finishing_near_timeout_no_error(tmp_path: Path) -> None:
    """A process that exits at roughly the same moment as the timeout must not raise."""
    runner = ProcessRunner(cancel_grace_seconds=1)
    log_path = tmp_path / "race.log"

    # Sleep just under the timeout so exit and timeout race each other.
    result = await runner.run_logged(["/bin/sleep", "0.9"], log_path=log_path, timeout_seconds=1)
    # Whether the process wins or the timeout fires first, we must get a
    # ProcessResult — never an unhandled exception.
    assert isinstance(result, ProcessResult)
    # Either the process finished normally or timed out; both are acceptable.
    assert result.exit_code is not None


@pytest.mark.asyncio
async def test_run_capture_normal_command_returns_output() -> None:
    """A simple, fast command returns its stdout through run_capture."""
    runner = ProcessRunner()
    code, output = await runner.run_capture(["/bin/echo", "hello"], timeout_seconds=5)
    assert code == 0
    assert output.strip() == "hello"
