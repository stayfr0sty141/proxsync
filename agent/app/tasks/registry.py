"""Task registry with an on-disk journal.

The dashboard polls ``GET /task/{id}``. If the agent restarts between the poll and the task
finishing, the dashboard must still get a truthful answer rather than a 404 — so every state
transition is written to ``state_dir/tasks/<id>.json`` atomically (temp file + ``os.replace``).

On startup, any task still marked ``running`` is reconciled to ``interrupted``: systemd kills
the agent's whole cgroup, so its children did not survive, and claiming otherwise would let
the dashboard record a backup that never completed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.core.errors import NotFound
from app.core.logging import logger
from app.executors.base import ProcessHandle
from app.schemas.enums import TaskKind, TaskState
from app.tasks.models import Task, TaskProgress, utcnow


class TaskRegistry:
    def __init__(
        self,
        *,
        journal_dir: Path,
        log_dir: Path,
        retention_hours: int = 336,
    ) -> None:
        self._journal_dir = journal_dir
        self._log_dir = log_dir
        self._retention = timedelta(hours=retention_hours)
        self._tasks: dict[str, Task] = {}
        self._handles: dict[str, ProcessHandle] = {}

    # ---- lifecycle -----------------------------------------------------------

    def load(self) -> int:
        """Load journalled tasks and reconcile anything left running. Returns the count loaded."""
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        recovered = 0
        for path in sorted(self._journal_dir.glob("*.json")):
            try:
                task = Task.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                logger.warning("task_journal_unreadable", path=str(path), exc_info=True)
                continue

            if task.state in {TaskState.RUNNING, TaskState.QUEUED}:
                task.state = TaskState.INTERRUPTED
                task.finished_at = utcnow()
                task.error = (
                    "The agent restarted while this task was running; its outcome is unknown. "
                    "Inspect the task log and the backup storage before retrying."
                )
                self._persist(task)
                logger.warning("task_interrupted_by_restart", task_id=task.id, kind=task.kind)

            self._tasks[task.id] = task
            recovered += 1
        return recovered

    def prune(self) -> int:
        """Drop terminal tasks older than the retention window. Returns the count removed."""
        cutoff = utcnow() - self._retention
        removed = 0
        for task_id, task in list(self._tasks.items()):
            reference = task.finished_at or task.created_at
            if task.state.is_terminal and reference < cutoff:
                self._tasks.pop(task_id, None)
                self._handles.pop(task_id, None)
                self._journal_path(task_id).unlink(missing_ok=True)
                removed += 1
        return removed

    # ---- accessors -----------------------------------------------------------

    def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise NotFound(f"No task with id {task_id}")
        return task

    def find(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self, *, kind: TaskKind | None = None, state: TaskState | None = None) -> list[Task]:
        tasks: Iterator[Task] = iter(self._tasks.values())
        if kind is not None:
            tasks = (task for task in tasks if task.kind is kind)
        if state is not None:
            tasks = (task for task in tasks if task.state is state)
        return sorted(tasks, key=lambda task: task.created_at, reverse=True)

    def active_count(self, kind: TaskKind) -> int:
        return sum(
            1
            for task in self._tasks.values()
            if task.kind is kind and task.state in {TaskState.QUEUED, TaskState.RUNNING}
        )

    # ---- mutations -----------------------------------------------------------

    def create(
        self,
        *,
        kind: TaskKind,
        correlation_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Task:
        task_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            kind=kind,
            state=TaskState.QUEUED,
            log_path=self._log_dir / f"{task_id}.log",
            correlation_id=correlation_id,
            meta=meta or {},
        )
        self._tasks[task_id] = task
        self._persist(task)
        logger.info("task_created", task_id=task_id, kind=kind.value, **task.meta)
        return task

    def attach_handle(self, task_id: str, handle: ProcessHandle) -> None:
        self._handles[task_id] = handle

    def handle(self, task_id: str) -> ProcessHandle | None:
        return self._handles.get(task_id)

    def mark_running(self, task: Task, *, pid: int | None = None) -> None:
        task.state = TaskState.RUNNING
        task.started_at = task.started_at or utcnow()
        task.pid = pid
        self._persist(task)

    def update_progress(self, task: Task, progress: TaskProgress, *, persist: bool = False) -> None:
        task.progress = progress
        if persist:
            self._persist(task)

    def finish(
        self,
        task: Task,
        *,
        state: TaskState,
        exit_code: int | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        task.state = state
        task.exit_code = exit_code
        task.error = error
        task.finished_at = utcnow()
        task.pid = None
        if result:
            task.result.update(result)
        self._handles.pop(task.id, None)
        self._persist(task)
        logger.info(
            "task_finished",
            task_id=task.id,
            kind=task.kind.value,
            state=state.value,
            exit_code=exit_code,
            duration=task.duration_seconds,
        )

    # ---- persistence ---------------------------------------------------------

    def _journal_path(self, task_id: str) -> Path:
        return self._journal_dir / f"{task_id}.json"

    def _persist(self, task: Task) -> None:
        path = self._journal_path(task.id)
        temporary = path.with_suffix(".json.tmp")
        payload = json.dumps(task.as_dict(), indent=2)
        try:
            try:
                temporary.write_text(payload, encoding="utf-8")
            except FileNotFoundError:
                # First write of a fresh install, before the state directory exists.
                self._journal_dir.mkdir(parents=True, exist_ok=True)
                temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # A journal write failure must not abort a running backup; it degrades restart
            # recovery only, and that is worth logging loudly rather than raising.
            logger.error("task_journal_write_failed", task_id=task.id, exc_info=True)
            temporary.unlink(missing_ok=True)
