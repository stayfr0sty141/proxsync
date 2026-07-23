"""vzdump argv construction.

Nothing here interpolates a string into a command. Every value has already been validated by
:mod:`app.validators`; this module only decides which flags express the request.

Note on compression: vzdump exposes ``--compress {0,gzip,lzo,zstd}`` and, for zstd, a
``--zstd`` *thread count* — there is no compression-level knob. The agent therefore accepts
``zstd_threads`` (0 = half the host's cores) rather than inventing a level that PVE would
ignore.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.schemas.enums import BackupMode, Compression


@dataclass(frozen=True, slots=True)
class VzdumpCommand:
    vmid: int
    mode: BackupMode
    compression: Compression
    storage: str
    zstd_threads: int | None = None
    bwlimit_kbps: int | None = None
    tmpdir: Path | None = None

    def build(self, vzdump_bin: Path) -> list[str]:
        argv = [
            str(vzdump_bin),
            str(self.vmid),
            "--mode",
            self.mode.value,
            "--compress",
            self.compression.vzdump_value,
            "--storage",
            self.storage,
        ]
        if self.compression is Compression.ZSTD and self.zstd_threads is not None:
            argv += ["--zstd", str(self.zstd_threads)]
        if self.bwlimit_kbps:
            argv += ["--bwlimit", str(self.bwlimit_kbps)]
        if self.tmpdir is not None:
            argv += ["--tmpdir", str(self.tmpdir)]
        return argv
