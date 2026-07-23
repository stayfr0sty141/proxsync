"""rclone argv construction and output parsing.

Decision D1 puts rclone on the host rather than in the dashboard container: the artifacts are
here, and copying 40 GiB through the LXC to reach Google Drive would double the network cost
for nothing. The dashboard reaches it through the typed endpoints in `app.api.routes.sync`.

Two flags deserve explanation:

``--retries 1``
    rclone's own retry loop is switched **off**. The dashboard counts attempts, applies its
    own backoff, and records each one in `sync_tasks`; letting rclone silently retry three
    times underneath would make that record a lie and the backoff meaningless.

``--low-level-retries``
    Left on. This retries a single failed HTTP chunk within one transfer, which is a
    different thing entirely — it is what makes a 40 GiB upload survive a dropped packet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.errors import ExecutionFailed
from app.tasks.models import TaskProgress

_UNIT_FACTORS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "ki": 1024,
    "kib": 1024,
    "kbytes": 1000,
    "kb": 1000,
    "m": 1024**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "mbytes": 1000**2,
    "mb": 1000**2,
    "g": 1024**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "gbytes": 1000**3,
    "gb": 1000**3,
    "t": 1024**4,
    "ti": 1024**4,
    "tib": 1024**4,
    "tbytes": 1000**4,
    "tb": 1000**4,
}

# Transferred:   	    1.234 GiB / 5.000 GiB, 25%, 45.123 MiB/s, ETA 1m23s
_STATS_LINE = re.compile(
    r"Transferred:\s*(?P<done>[\d.]+)\s*(?P<done_unit>[KMGT]?i?B(?:ytes)?)\s*/\s*"
    r"(?P<total>[\d.]+)\s*(?P<total_unit>[KMGT]?i?B(?:ytes)?)\s*,\s*"
    r"(?P<percent>\d+)\s*%"
    r"(?:\s*,\s*(?P<rate>[\d.]+)\s*(?P<rate_unit>[KMGT]?i?B(?:ytes)?)/s)?"
    r"(?:\s*,\s*ETA\s+(?P<eta>[\dhms.-]+))?",
    re.IGNORECASE,
)
# Older/short form, emitted when the total is not yet known.
_STATS_BYTES_ONLY = re.compile(
    r"Transferred:\s*(?P<done>[\d.]+)\s*(?P<done_unit>[KMGT]?i?B(?:ytes)?)\s*"
    r"\(\s*(?P<rate>[\d.]+)\s*(?P<rate_unit>[KMGT]?i?B(?:ytes)?)/s\s*\)",
    re.IGNORECASE,
)
_ETA = re.compile(r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?")
_ERROR_LINE = re.compile(r"(?:ERROR|CRITICAL)\s*:\s*(?P<message>.+)$")


def to_bytes(value: str, unit: str) -> int:
    # `removesuffix`, not `rstrip("/s")`: rstrip strips *any* trailing '/' or 's', which turns
    # "GBytes" into "gbyte" and silently yields a factor of 1 — a 1.2 GiB transfer reported as
    # 1 byte.
    normalised = unit.strip().lower().removesuffix("/s")
    factor = _UNIT_FACTORS.get(normalised, 1)
    return int(float(value) * factor)


def parse_eta(value: str) -> int | None:
    """``1m23s`` → 83. rclone writes ``-`` when it cannot estimate."""
    if not value or value == "-":
        return None
    match = _ETA.fullmatch(value.strip())
    if match is None or not any(match.groups()):
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


@dataclass(slots=True)
class RcloneProgressParser:
    """Turns ``--stats-one-line-date`` output into task progress."""

    error_message: str | None = None

    def feed(self, line: str, progress: TaskProgress) -> bool:
        if match := _STATS_LINE.search(line):
            progress.percent = float(match.group("percent"))
            progress.bytes_done = to_bytes(match.group("done"), match.group("done_unit"))
            progress.bytes_total = to_bytes(match.group("total"), match.group("total_unit"))
            if match.group("rate"):
                progress.rate_bps = to_bytes(match.group("rate"), match.group("rate_unit"))
            progress.eta_seconds = parse_eta(match.group("eta") or "")
            progress.message = "transferring"
            return True

        if match := _STATS_BYTES_ONLY.search(line):
            progress.bytes_done = to_bytes(match.group("done"), match.group("done_unit"))
            progress.rate_bps = to_bytes(match.group("rate"), match.group("rate_unit"))
            progress.message = "transferring"
            return True

        if match := _ERROR_LINE.search(line):
            self.error_message = match.group("message").strip()
            return True

        return False


@dataclass(frozen=True, slots=True)
class RcloneOptions:
    """Flags shared by every invocation."""

    rclone_bin: Path
    config_path: Path | None = None
    transfers: int = 4
    checkers: int = 8
    bwlimit_kbps: int = 0
    low_level_retries: int = 3
    stats_interval_seconds: int = 5
    extra: tuple[str, ...] = field(default_factory=tuple)

    def base_argv(self) -> list[str]:
        argv = [str(self.rclone_bin)]
        if self.config_path is not None:
            argv += ["--config", str(self.config_path)]
        argv += [
            # One attempt. The dashboard owns retries so its attempt counter means something.
            "--retries",
            "1",
            "--low-level-retries",
            str(self.low_level_retries),
        ]
        if self.bwlimit_kbps:
            argv += ["--bwlimit", f"{self.bwlimit_kbps}k"]
        argv += list(self.extra)
        return argv

    def transfer_argv(self) -> list[str]:
        return [
            *self.base_argv(),
            "--transfers",
            str(self.transfers),
            "--checkers",
            str(self.checkers),
            "--stats",
            f"{self.stats_interval_seconds}s",
            "--stats-one-line-date",
            "--stats-log-level",
            "NOTICE",
        ]


def build_copy_argv(options: RcloneOptions, *, source: str, destination: str) -> list[str]:
    """``copyto`` rather than ``copy``: the destination name is stated, not inferred.

    ``copy`` treats the destination as a directory, so a remote path that does not exist yet
    produces a *directory* of that name. `copyto` is unambiguous in both directions.
    """
    return [*options.transfer_argv(), "copyto", source, destination]


def build_lsjson_argv(
    options: RcloneOptions, *, target: str, with_hashes: bool = True
) -> list[str]:
    argv = [*options.base_argv(), "lsjson"]
    if with_hashes:
        argv.append("--hash")
    argv.append(target)
    return argv


def build_about_argv(options: RcloneOptions, *, remote_spec: str) -> list[str]:
    return [*options.base_argv(), "about", "--json", remote_spec]


def build_delete_argv(options: RcloneOptions, *, target: str) -> list[str]:
    """``deletefile`` refuses a directory, so a wrong path cannot remove a whole folder."""
    return [*options.base_argv(), "deletefile", target]


def build_mkdir_argv(options: RcloneOptions, *, target: str) -> list[str]:
    return [*options.base_argv(), "mkdir", target]


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    name: str
    path: str
    size_bytes: int
    is_dir: bool
    modified_at: datetime | None
    hashes: dict[str, str]

    @property
    def md5(self) -> str | None:
        """Google Drive stores MD5, and it is the only hash it will return without a download."""
        return self.hashes.get("md5") or self.hashes.get("MD5")


def parse_lsjson(output: str) -> list[RemoteEntry]:
    try:
        payload = json.loads(output or "[]")
    except ValueError as exc:
        raise ExecutionFailed(f"rclone lsjson returned invalid JSON: {output[:200]}") from exc

    if not isinstance(payload, list):
        raise ExecutionFailed("rclone lsjson did not return a list")

    entries: list[RemoteEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        entries.append(
            RemoteEntry(
                name=str(item.get("Name", "")),
                path=str(item.get("Path", "")),
                size_bytes=int(item.get("Size") or 0),
                is_dir=bool(item.get("IsDir", False)),
                modified_at=_parse_time(item.get("ModTime")),
                hashes={
                    str(key): str(value)
                    for key, value in (item.get("Hashes") or {}).items()
                    if value
                },
            )
        )
    return entries


@dataclass(frozen=True, slots=True)
class RemoteQuota:
    total_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    trashed_bytes: int | None

    @property
    def used_percent(self) -> float | None:
        if not self.total_bytes or self.used_bytes is None:
            return None
        return round(self.used_bytes / self.total_bytes * 100, 2)


def parse_about(output: str) -> RemoteQuota:
    try:
        payload = json.loads(output or "{}")
    except ValueError as exc:
        raise ExecutionFailed(f"rclone about returned invalid JSON: {output[:200]}") from exc

    if not isinstance(payload, dict):
        raise ExecutionFailed("rclone about did not return an object")

    # Not every backend reports every field; a missing quota is None, never zero. Zero would
    # read as "no space left" on the storage page.
    return RemoteQuota(
        total_bytes=_optional_int(payload.get("total")),
        used_bytes=_optional_int(payload.get("used")),
        free_bytes=_optional_int(payload.get("free")),
        trashed_bytes=_optional_int(payload.get("trashed")),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
