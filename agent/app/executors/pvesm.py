"""``pvesm status`` invocation and parsing.

Output shape (values are in 1 KiB units):

    Name             Type     Status           Total            Used       Available        %
    backup-hdd        dir     active       488384352       312516608       151011744   64.00%
    local             dir     active        30832548         3288400        25955116   10.67%

Inactive storages report zeros; they are still listed so the caller can report them as
present-but-unavailable rather than missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ExecutionFailed
from app.executors.base import ProcessRunner

_KIB = 1024


@dataclass(frozen=True, slots=True)
class StorageStatus:
    name: str
    type: str
    active: bool
    total_bytes: int
    used_bytes: int
    available_bytes: int

    @property
    def used_percent(self) -> float:
        return round(self.used_bytes / self.total_bytes * 100, 2) if self.total_bytes else 0.0


def build_status_argv(pvesm_bin: Path) -> list[str]:
    return [str(pvesm_bin), "status"]


def parse_status(output: str) -> list[StorageStatus]:
    entries: list[StorageStatus] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("Name"):
            continue
        columns = line.split()
        if len(columns) < 6:
            continue
        name, storage_type, status, total, used, available = columns[:6]
        try:
            total_kib, used_kib, available_kib = int(total), int(used), int(available)
        except ValueError:
            continue
        entries.append(
            StorageStatus(
                name=name,
                type=storage_type,
                active=status == "active",
                total_bytes=total_kib * _KIB,
                used_bytes=used_kib * _KIB,
                available_bytes=available_kib * _KIB,
            )
        )
    return entries


class PvesmClient:
    def __init__(self, *, runner: ProcessRunner, pvesm_bin: Path, timeout_seconds: int) -> None:
        self._runner = runner
        self._bin = pvesm_bin
        self._timeout = timeout_seconds

    async def status(self) -> list[StorageStatus]:
        exit_code, output = await self._runner.run_capture(
            build_status_argv(self._bin), timeout_seconds=self._timeout
        )
        if exit_code != 0:
            raise ExecutionFailed(f"pvesm status exited {exit_code}: {output.strip()[:200]}")
        return parse_status(output)

    async def storage_names(self) -> list[str]:
        return [entry.name for entry in await self.status()]
