"""vzdump artifact naming.

The agent will only ever list, stream, restore from or delete files whose names match the
vzdump pattern. A file the agent did not produce is not an artifact and cannot be touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.errors import ValidationFailed
from app.schemas.enums import Compression, GuestType

# vzdump-qemu-101-2026_07_19-01_00_04.vma.zst
# vzdump-lxc-104-2026_07_19-01_07_22.tar.zst
ARTIFACT_PATTERN = re.compile(
    r"^vzdump-(?P<kind>qemu|lxc|openvz)-(?P<vmid>\d{1,9})-"
    r"(?P<stamp>\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})"
    r"\.(?P<container>vma|tar)"
    r"(?:\.(?P<compression>zst|gz|lzo))?$"
)

_KIND_TO_GUEST_TYPE = {"qemu": GuestType.VM, "lxc": GuestType.LXC, "openvz": GuestType.LXC}
_SUFFIX_TO_COMPRESSION = {
    None: Compression.NONE,
    "zst": Compression.ZSTD,
    "gz": Compression.GZIP,
    "lzo": Compression.LZO,
}
_TIMESTAMP_FORMAT = "%Y_%m_%d-%H_%M_%S"


@dataclass(frozen=True, slots=True)
class ArtifactName:
    """A parsed, trusted vzdump filename."""

    filename: str
    guest_type: GuestType
    vmid: int
    created_at: datetime
    container_format: str
    compression: Compression

    @property
    def stem(self) -> str:
        """``vzdump-qemu-101-2026_07_19-01_00_04`` — the base PVE uses for the ``.log`` sidecar."""
        return self.filename.split(f".{self.container_format}", 1)[0]


def parse_artifact_name(filename: str) -> ArtifactName:
    match = ARTIFACT_PATTERN.match(filename)
    if match is None:
        raise ValidationFailed(
            f"'{filename}' is not a vzdump artifact name; refusing to operate on it"
        )

    stamp = match.group("stamp")
    try:
        created_at = datetime.strptime(stamp, _TIMESTAMP_FORMAT)  # noqa: DTZ007 - host-local by design
    except ValueError:
        raise ValidationFailed(f"'{filename}' carries an invalid timestamp") from None

    return ArtifactName(
        filename=filename,
        guest_type=_KIND_TO_GUEST_TYPE[match.group("kind")],
        vmid=int(match.group("vmid")),
        created_at=created_at,
        container_format=match.group("container"),
        compression=_SUFFIX_TO_COMPRESSION[match.group("compression")],
    )


def is_artifact_name(filename: str) -> bool:
    return ARTIFACT_PATTERN.match(filename) is not None


def sidecar_paths(artifact: ArtifactName, artifact_path: Path) -> list[Path]:
    """Companion files PVE and ProxSync write beside an artifact.

    PVE writes ``<stem>.log`` and ``<filename>.notes``; ProxSync adds ``<filename>.sha256``
    and, once the artifact has been compared against a remote, ``<filename>.md5``.
    Only files that actually exist are returned.
    """
    directory = artifact_path.parent
    candidates = [
        directory / f"{artifact.stem}.log",
        directory / f"{artifact.filename}.notes",
        directory / f"{artifact.filename}.sha256",
        directory / f"{artifact.filename}.md5",
        directory / f"{artifact.filename}.log",
    ]
    return [path for path in candidates if path.exists()]
