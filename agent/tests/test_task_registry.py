"""Task registry: journalling, restart recovery, pruning."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.errors import NotFound
from app.schemas.enums import TaskKind, TaskState
from app.tasks.models import TaskProgress, utcnow
from app.tasks.registry import TaskRegistry


@pytest.fixture
def registry(tmp_path: Path) -> TaskRegistry:
    return TaskRegistry(
        journal_dir=tmp_path / "tasks",
        log_dir=tmp_path / "logs",
        retention_hours=24,
    )


def test_create_writes_a_journal_entry(registry: TaskRegistry, tmp_path: Path) -> None:
    task = registry.create(kind=TaskKind.BACKUP, meta={"vmid": 101})
    journal = tmp_path / "tasks" / f"{task.id}.json"

    assert journal.is_file()
    stored = json.loads(journal.read_text(encoding="utf-8"))
    assert stored["kind"] == "backup"
    assert stored["state"] == "queued"
    assert stored["meta"]["vmid"] == 101


def test_get_unknown_task_raises(registry: TaskRegistry) -> None:
    with pytest.raises(NotFound):
        registry.get("00000000-0000-0000-0000-000000000000")


def test_finish_records_terminal_state(registry: TaskRegistry) -> None:
    task = registry.create(kind=TaskKind.BACKUP)
    registry.mark_running(task)
    registry.finish(
        task,
        state=TaskState.SUCCESS,
        exit_code=0,
        result={"filename": "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"},
    )

    reloaded = registry.get(task.id)
    assert reloaded.state is TaskState.SUCCESS
    assert reloaded.exit_code == 0
    assert reloaded.finished_at is not None
    assert reloaded.duration_seconds is not None
    assert reloaded.result["filename"].startswith("vzdump-qemu-101")


def test_running_tasks_become_interrupted_after_restart(
    registry: TaskRegistry, tmp_path: Path
) -> None:
    task = registry.create(kind=TaskKind.BACKUP, meta={"vmid": 101})
    registry.mark_running(task)

    # A fresh registry over the same journal simulates an agent restart.
    restarted = TaskRegistry(
        journal_dir=tmp_path / "tasks", log_dir=tmp_path / "logs", retention_hours=24
    )
    assert restarted.load() == 1

    recovered = restarted.get(task.id)
    assert recovered.state is TaskState.INTERRUPTED
    assert recovered.finished_at is not None
    assert recovered.error is not None
    assert "restarted" in recovered.error


def test_terminal_tasks_survive_restart_unchanged(registry: TaskRegistry, tmp_path: Path) -> None:
    task = registry.create(kind=TaskKind.RESTORE_VM)
    registry.finish(task, state=TaskState.SUCCESS, exit_code=0)

    restarted = TaskRegistry(
        journal_dir=tmp_path / "tasks", log_dir=tmp_path / "logs", retention_hours=24
    )
    restarted.load()
    assert restarted.get(task.id).state is TaskState.SUCCESS


def test_load_skips_corrupt_journal_files(registry: TaskRegistry, tmp_path: Path) -> None:
    good = registry.create(kind=TaskKind.BACKUP)
    registry.finish(good, state=TaskState.SUCCESS, exit_code=0)
    (tmp_path / "tasks" / "corrupt.json").write_text("{not json", encoding="utf-8")

    restarted = TaskRegistry(
        journal_dir=tmp_path / "tasks", log_dir=tmp_path / "logs", retention_hours=24
    )
    assert restarted.load() == 1


def test_prune_removes_old_terminal_tasks_only(registry: TaskRegistry, tmp_path: Path) -> None:
    old = registry.create(kind=TaskKind.BACKUP)
    registry.finish(old, state=TaskState.SUCCESS, exit_code=0)
    old.finished_at = utcnow() - timedelta(hours=48)

    recent = registry.create(kind=TaskKind.BACKUP)
    registry.finish(recent, state=TaskState.FAILED, exit_code=1)

    running = registry.create(kind=TaskKind.BACKUP)
    registry.mark_running(running)

    assert registry.prune() == 1
    assert not (tmp_path / "tasks" / f"{old.id}.json").is_file()
    assert registry.find(old.id) is None
    assert registry.find(recent.id) is not None
    assert registry.find(running.id) is not None


def test_list_filters_and_orders_newest_first(registry: TaskRegistry) -> None:
    first = registry.create(kind=TaskKind.BACKUP)
    second = registry.create(kind=TaskKind.RESTORE_VM)
    second.created_at = first.created_at + timedelta(seconds=5)

    assert [task.id for task in registry.list()] == [second.id, first.id]
    assert [task.id for task in registry.list(kind=TaskKind.BACKUP)] == [first.id]
    assert registry.list(state=TaskState.SUCCESS) == []


def test_active_count_tracks_queued_and_running(registry: TaskRegistry) -> None:
    queued = registry.create(kind=TaskKind.BACKUP)
    running = registry.create(kind=TaskKind.BACKUP)
    registry.mark_running(running)
    done = registry.create(kind=TaskKind.BACKUP)
    registry.finish(done, state=TaskState.SUCCESS, exit_code=0)

    assert registry.active_count(TaskKind.BACKUP) == 2
    assert queued.state is TaskState.QUEUED


def test_progress_updates_are_persisted_on_request(registry: TaskRegistry) -> None:
    task = registry.create(kind=TaskKind.BACKUP)
    registry.update_progress(task, TaskProgress(percent=42.0, bytes_done=100), persist=True)

    stored = json.loads((registry._journal_path(task.id)).read_text(encoding="utf-8"))  # noqa: SLF001
    assert stored["progress"]["percent"] == 42.0
