"""Task records — the agent's only persistent state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas.enums import TaskKind, TaskState


def utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


@dataclass(slots=True)
class TaskProgress:
    percent: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    rate_bps: int | None = None
    eta_seconds: int | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TaskProgress:
        if not data:
            return cls()
        return cls(**{key: data.get(key) for key in cls.__slots__})


@dataclass(slots=True)
class Task:
    id: str
    kind: TaskKind
    state: TaskState
    log_path: Path
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    correlation_id: str | None = None
    progress: TaskProgress = field(default_factory=TaskProgress)
    result: dict[str, Any] = field(default_factory=dict)
    """Task output the dashboard needs: filename, size_bytes, checksum_sha256, target_vmid…"""
    meta: dict[str, Any] = field(default_factory=dict)
    """Request context: vmid, guest_type, storage, mode…"""

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return round((end - self.started_at).total_seconds(), 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "log_path": str(self.log_path),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "error": self.error,
            "correlation_id": self.correlation_id,
            "progress": self.progress.as_dict(),
            "result": dict(self.result),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        created_at = _parse_dt(data.get("created_at")) or utcnow()
        return cls(
            id=str(data["id"]),
            kind=TaskKind(data["kind"]),
            state=TaskState(data["state"]),
            log_path=Path(data["log_path"]),
            created_at=created_at,
            started_at=_parse_dt(data.get("started_at")),
            finished_at=_parse_dt(data.get("finished_at")),
            pid=data.get("pid"),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
            correlation_id=data.get("correlation_id"),
            progress=TaskProgress.from_dict(data.get("progress")),
            result=dict(data.get("result") or {}),
            meta=dict(data.get("meta") or {}),
        )
