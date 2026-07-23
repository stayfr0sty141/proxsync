"""Restore and guest-lifecycle argv construction.

``qmrestore`` and ``pct restore`` take their positional arguments in *different* orders:

    qmrestore <archive> <vmid> --storage <storage>
    pct restore <vmid> <archive> --storage <storage>

Getting that backwards would either fail loudly or, worse, be interpreted oddly — so each has
its own builder with its own test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.schemas.enums import GuestType


@dataclass(frozen=True, slots=True)
class RestoreCommand:
    archive_path: Path
    target_vmid: int
    storage: str
    overwrite: bool = False
    bwlimit_kbps: int | None = None
    unprivileged: bool | None = None  # LXC only

    def build_vm(self, qmrestore_bin: Path) -> list[str]:
        argv = [
            str(qmrestore_bin),
            str(self.archive_path),
            str(self.target_vmid),
            "--storage",
            self.storage,
        ]
        if self.overwrite:
            argv += ["--force", "1"]
        if self.bwlimit_kbps:
            argv += ["--bwlimit", str(self.bwlimit_kbps)]
        return argv

    def build_lxc(self, pct_bin: Path) -> list[str]:
        argv = [
            str(pct_bin),
            "restore",
            str(self.target_vmid),
            str(self.archive_path),
            "--storage",
            self.storage,
        ]
        if self.overwrite:
            argv += ["--force", "1"]
        if self.bwlimit_kbps:
            argv += ["--bwlimit", str(self.bwlimit_kbps)]
        if self.unprivileged is not None:
            argv += ["--unprivileged", "1" if self.unprivileged else "0"]
        return argv


def build_status_argv(binary: Path, guest_type: GuestType, vmid: int) -> list[str]:
    """``qm status <vmid>`` / ``pct status <vmid>``."""
    del guest_type  # both tools use the same sub-command shape
    return [str(binary), "status", str(vmid)]


def build_stop_argv(binary: Path, vmid: int) -> list[str]:
    return [str(binary), "stop", str(vmid)]


def build_start_argv(binary: Path, vmid: int) -> list[str]:
    return [str(binary), "start", str(vmid)]


def parse_status_output(output: str) -> str:
    """``status: running`` → ``running``. Unrecognised output reads as ``unknown``."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("status:"):
            return stripped.split(":", 1)[1].strip()
    return "unknown"
