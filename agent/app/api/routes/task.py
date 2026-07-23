"""Task inspection, log retrieval and cancellation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import ContainerDep
from app.core.errors import NotFound, ValidationFailed
from app.schemas.enums import TaskKind, TaskState
from app.schemas.responses import TaskLogResponse, TaskResponse

router = APIRouter(prefix="/task", tags=["task"])

TaskId = Annotated[str, Path(min_length=8, max_length=64, pattern=r"^[0-9a-fA-F-]{8,64}$")]


@router.get("", summary="List tasks")
async def list_tasks(
    container: ContainerDep,
    kind: TaskKind | None = None,
    state: TaskState | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TaskResponse]:
    tasks = container.registry.list(kind=kind, state=state)[:limit]
    return [TaskResponse.from_task(task) for task in tasks]


@router.get("/{task_id}", summary="Get task state")
async def get_task(task_id: TaskId, container: ContainerDep) -> TaskResponse:
    return TaskResponse.from_task(container.registry.get(task_id))


@router.get("/{task_id}/log", summary="Read a task log")
async def get_task_log(
    task_id: TaskId,
    container: ContainerDep,
    tail: Annotated[int, Query(ge=1)] = 500,
) -> TaskLogResponse:
    task = container.registry.get(task_id)
    limit = min(tail, container.settings.max_log_tail_lines)

    if not task.log_path.is_file():
        raise NotFound(f"No log file exists for task {task_id}")

    try:
        content = task.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise NotFound(f"Task log could not be read: {exc}") from exc

    lines = content.splitlines()
    return TaskLogResponse(
        task_id=task_id,
        lines=lines[-limit:],
        truncated=len(lines) > limit,
        total_lines=len(lines),
    )


@router.post("/{task_id}/cancel", summary="Cancel a running task")
async def cancel_task(task_id: TaskId, container: ContainerDep) -> TaskResponse:
    task = container.registry.get(task_id)
    if task.state.is_terminal:
        raise ValidationFailed(f"Task {task_id} already finished with state '{task.state.value}'")

    service = (
        container.backup_service if task.kind is TaskKind.BACKUP else container.restore_service
    )
    if not await service.cancel(task_id):
        raise NotFound(f"Task {task_id} has no cancellable process")

    return TaskResponse.from_task(container.registry.get(task_id))
