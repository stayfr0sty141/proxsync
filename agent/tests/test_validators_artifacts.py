"""Artifact naming. A file the agent cannot parse is a file the agent will not touch."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ValidationFailed
from app.schemas.enums import Compression, GuestType
from app.validators.artifacts import is_artifact_name, parse_artifact_name, sidecar_paths


@pytest.mark.parametrize(
    ("filename", "guest_type", "vmid", "compression"),
    [
        ("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst", GuestType.VM, 101, Compression.ZSTD),
        ("vzdump-lxc-104-2026_07_19-01_07_22.tar.zst", GuestType.LXC, 104, Compression.ZSTD),
        ("vzdump-qemu-100-2024_01_01-00_00_00.vma", GuestType.VM, 100, Compression.NONE),
        ("vzdump-lxc-108-2025_12_31-23_59_59.tar.gz", GuestType.LXC, 108, Compression.GZIP),
        ("vzdump-qemu-999-2025_06_15-12_30_00.vma.lzo", GuestType.VM, 999, Compression.LZO),
    ],
)
def test_parse_valid_artifacts(
    filename: str, guest_type: GuestType, vmid: int, compression: Compression
) -> None:
    parsed = parse_artifact_name(filename)
    assert parsed.guest_type is guest_type
    assert parsed.vmid == vmid
    assert parsed.compression is compression
    assert parsed.filename == filename


@pytest.mark.parametrize(
    "filename",
    [
        "important-database.sql",
        "vzdump-qemu-101.vma.zst",
        "vzdump-qemu-101-2026_07_19.vma.zst",
        "vzdump-qemu-abc-2026_07_19-01_00_04.vma.zst",
        "vzdump-docker-101-2026_07_19-01_00_04.vma.zst",
        "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst.bak",
        "vzdump-qemu-101-2026_07_19-01_00_04.iso",
        "vzdump-qemu-101-2026_07_19-01_00_04.vma.xz",
        "/etc/passwd",
        "",
    ],
)
def test_reject_non_artifacts(filename: str) -> None:
    assert not is_artifact_name(filename)
    with pytest.raises(ValidationFailed):
        parse_artifact_name(filename)


def test_reject_impossible_timestamp() -> None:
    with pytest.raises(ValidationFailed, match="invalid timestamp"):
        parse_artifact_name("vzdump-qemu-101-2026_13_45-99_00_04.vma.zst")


def test_stem_matches_pve_log_naming() -> None:
    parsed = parse_artifact_name("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst")
    assert parsed.stem == "vzdump-qemu-101-2026_07_19-01_00_04"


def test_sidecar_paths_returns_only_existing_files(tmp_path: Path) -> None:
    filename = "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"
    artifact_path = tmp_path / filename
    artifact_path.write_bytes(b"payload")
    (tmp_path / "vzdump-qemu-101-2026_07_19-01_00_04.log").write_text("log", encoding="utf-8")
    (tmp_path / f"{filename}.notes").write_text("notes", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("keep me", encoding="utf-8")

    parsed = parse_artifact_name(filename)
    found = {path.name for path in sidecar_paths(parsed, artifact_path)}

    assert found == {
        "vzdump-qemu-101-2026_07_19-01_00_04.log",
        f"{filename}.notes",
    }
