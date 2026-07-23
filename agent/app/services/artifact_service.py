"""Listing, inspecting and deleting vzdump artifacts.

The dump root is treated as an untrusted directory: anything that is not a well-formed vzdump
artifact is invisible to this service and cannot be deleted through it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.core.config import AgentSettings
from app.core.errors import NotFound
from app.core.logging import logger
from app.executors.checksum import read_cached
from app.schemas.enums import GuestType
from app.schemas.responses import ArtifactResponse, DeletedArtifactResponse
from app.validators.artifacts import (
    ArtifactName,
    is_artifact_name,
    parse_artifact_name,
    sidecar_paths,
)
from app.validators.paths import resolve_within

_MAX_NOTES_BYTES = 4096


class ArtifactService:
    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    @property
    def dump_root(self) -> Path:
        return self._settings.dump_root

    def resolve(self, filename: str) -> tuple[ArtifactName, Path]:
        """Validate the name, confirm containment, and confirm the file exists."""
        artifact = parse_artifact_name(filename)
        path = resolve_within(self.dump_root, filename)
        if not path.is_file():
            raise NotFound(f"Backup '{filename}' does not exist on this host")
        return artifact, path

    def list(
        self, *, vmid: int | None = None, guest_type: GuestType | None = None
    ) -> list[ArtifactResponse]:
        if not self.dump_root.is_dir():
            raise NotFound(f"Backup directory {self.dump_root} does not exist")

        entries: list[ArtifactResponse] = []
        for path in self.dump_root.iterdir():
            if not path.is_file() or not is_artifact_name(path.name):
                continue
            artifact = parse_artifact_name(path.name)
            if vmid is not None and artifact.vmid != vmid:
                continue
            if guest_type is not None and artifact.guest_type is not guest_type:
                continue
            try:
                stat = path.stat()
            except OSError:  # removed between iterdir() and stat()
                continue

            entries.append(
                ArtifactResponse(
                    filename=path.name,
                    path=str(path),
                    vmid=artifact.vmid,
                    guest_type=artifact.guest_type,
                    size_bytes=stat.st_size,
                    created_at=artifact.created_at.astimezone(),
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    compression=artifact.compression,
                    checksum_sha256=read_cached(path),
                    notes=_read_notes(path),
                )
            )

        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return entries

    def delete(self, filename: str) -> DeletedArtifactResponse:
        artifact, path = self.resolve(filename)
        targets = [path, *sidecar_paths(artifact, path)]

        freed = 0
        deleted: list[str] = []
        for target in targets:
            try:
                freed += target.stat().st_size
                target.unlink()
                deleted.append(target.name)
            except FileNotFoundError:
                continue
            except OSError:
                logger.error("artifact_delete_failed", path=str(target), exc_info=True)
                raise

        logger.info("artifact_deleted", filename=filename, freed_bytes=freed, files=deleted)
        return DeletedArtifactResponse(filename=filename, deleted=deleted, freed_bytes=freed)

    def usage(self) -> tuple[int, int]:
        """``(artifact_count, total_bytes)`` for the storage endpoint."""
        count = 0
        total = 0
        if not self.dump_root.is_dir():
            return 0, 0
        for path in self.dump_root.iterdir():
            if path.is_file() and is_artifact_name(path.name):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
                count += 1
        return count, total


def _read_notes(artifact_path: Path) -> str | None:
    notes_path = artifact_path.with_name(artifact_path.name + ".notes")
    try:
        if not notes_path.is_file():
            return None
        return notes_path.read_text(encoding="utf-8", errors="replace")[:_MAX_NOTES_BYTES].strip()
    except OSError:
        return None
