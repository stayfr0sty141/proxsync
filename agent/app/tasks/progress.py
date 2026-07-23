"""Parsers that turn command output into structured progress.

These are pure functions over single lines so they can be tested against real captured
output without a Proxmox host. A line that matches nothing simply leaves progress unchanged —
the parser never guesses.

Observed vzdump output shapes:

    INFO: creating vzdump archive '/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst'
    INFO: status: 12% (3865470976/30601641984), sparse 0% (0), duration 33, read/write 117/117 MB/s
    INFO:  45% (1.2 GiB of 2.7 GiB) in 12s, read: 100 MiB/s, write: 90 MiB/s
    INFO: Total bytes written: 2274918400 (2.2GiB, 45MiB/s)
    INFO: archive file size: 8.50GB
    INFO: Finished Backup of VM 101 (00:06:12)
    ERROR: Backup of VM 103 failed - command 'qm guest ...' failed

LXC (tar) backups emit no percentage while running; the parser reports bytes instead of
inventing a percentage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.tasks.models import TaskProgress

_UNIT_FACTORS = {
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "tib": 1024**4,
}

_QEMU_STATUS = re.compile(
    r"status:\s*(?P<percent>\d+)%\s*\((?P<done>\d+)/(?P<total>\d+)\)"
    r"(?:.*?duration\s+(?P<duration>\d+))?"
    r"(?:.*?read/write\s+(?P<read>\d+)/(?P<write>\d+)\s*(?P<rate_unit>[KMG]B)/s)?",
    re.IGNORECASE,
)
_HUMAN_STATUS = re.compile(
    r"(?P<percent>\d{1,3})%\s*\((?P<done>[\d.]+)\s*(?P<done_unit>[KMGT]i?B)\s+of\s+"
    r"(?P<total>[\d.]+)\s*(?P<total_unit>[KMGT]i?B)\)",
    re.IGNORECASE,
)
_ARCHIVE_PATH = re.compile(r"creating (?:vzdump )?archive '(?P<path>[^']+)'", re.IGNORECASE)
_TOTAL_WRITTEN = re.compile(r"Total bytes written:\s*(?P<bytes>\d+)", re.IGNORECASE)
_ARCHIVE_SIZE = re.compile(
    r"archive file size:\s*(?P<size>[\d.]+)\s*(?P<unit>[KMGT]i?B)", re.IGNORECASE
)
_FAILED = re.compile(r"^ERROR:\s*(?P<message>.+)$")
_QMRESTORE_PROGRESS = re.compile(
    r"progress\s+(?P<percent>\d{1,3})%\s*\(read\s+(?P<done>\d+)\s+bytes,\s+"
    r"duration\s+(?P<duration>\d+)\s+sec\)",
    re.IGNORECASE,
)


def _to_bytes(value: str, unit: str) -> int:
    # `removesuffix`, not `rstrip("/s")`: rstrip strips any trailing '/' or 's', so a unit
    # spelled "GBytes" would silently degrade to a factor of 1.
    factor = _UNIT_FACTORS.get(unit.strip().lower().removesuffix("/s"), 1)
    return int(float(value) * factor)


@dataclass(slots=True)
class VzdumpProgressParser:
    """Stateful across lines: keeps the last known archive path and error message."""

    archive_path: str | None = None
    archive_size_bytes: int | None = None
    error_message: str | None = None

    def feed(self, line: str, progress: TaskProgress) -> bool:
        """Update ``progress`` in place. Returns True when something changed."""
        if match := _ARCHIVE_PATH.search(line):
            self.archive_path = match.group("path")
            progress.message = "writing archive"
            return True

        if match := _QEMU_STATUS.search(line):
            progress.percent = float(match.group("percent"))
            progress.bytes_done = int(match.group("done"))
            progress.bytes_total = int(match.group("total"))
            if match.group("write") and match.group("rate_unit"):
                progress.rate_bps = _to_bytes(match.group("write"), match.group("rate_unit"))
            progress.eta_seconds = _estimate_eta(progress)
            return True

        if match := _HUMAN_STATUS.search(line):
            progress.percent = float(match.group("percent"))
            progress.bytes_done = _to_bytes(match.group("done"), match.group("done_unit"))
            progress.bytes_total = _to_bytes(match.group("total"), match.group("total_unit"))
            progress.eta_seconds = _estimate_eta(progress)
            return True

        if match := _TOTAL_WRITTEN.search(line):
            progress.bytes_done = int(match.group("bytes"))
            progress.message = "archive written"
            return True

        if match := _ARCHIVE_SIZE.search(line):
            self.archive_size_bytes = _to_bytes(match.group("size"), match.group("unit"))
            return True

        if match := _FAILED.match(line.strip()):
            self.error_message = match.group("message").strip()
            return True

        return False


@dataclass(slots=True)
class RestoreProgressParser:
    error_message: str | None = None

    def feed(self, line: str, progress: TaskProgress) -> bool:
        if match := _QMRESTORE_PROGRESS.search(line):
            progress.percent = float(match.group("percent"))
            progress.bytes_done = int(match.group("done"))
            if progress.percent:
                progress.bytes_total = int(progress.bytes_done / (progress.percent / 100))
            progress.eta_seconds = _estimate_eta(progress)
            return True

        if match := _FAILED.match(line.strip()):
            self.error_message = match.group("message").strip()
            return True

        return False


def _estimate_eta(progress: TaskProgress) -> int | None:
    if not (progress.rate_bps and progress.bytes_total and progress.bytes_done is not None):
        return None
    remaining = progress.bytes_total - progress.bytes_done
    if remaining <= 0:
        return 0
    return int(remaining / progress.rate_bps)
