"""Path containment — the check that stands between a network request and the filesystem."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ValidationFailed
from app.validators.paths import assert_within, resolve_within, validate_basename

TRAVERSAL_ATTEMPTS = [
    "../etc/passwd",
    "../../../../etc/shadow",
    "..",
    ".",
    "",
    "   ",
    "/etc/passwd",
    "sub/dir/file.vma.zst",
    "dir\\file.vma.zst",
    "file\x00.vma.zst",
    "file\nname.vma.zst",
    "file\tname.vma.zst",
    "-rf",
    "--force",
    "a" * 256,
]


@pytest.mark.parametrize("name", TRAVERSAL_ATTEMPTS)
def test_validate_basename_rejects_hostile_names(name: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_basename(name)


@pytest.mark.parametrize(
    "name",
    [
        "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
        "vzdump-lxc-104-2026_07_19-01_07_22.tar.zst",
        "file.with.many.dots",
        "UPPER_case-123",
    ],
)
def test_validate_basename_accepts_plain_names(name: str) -> None:
    assert validate_basename(name) == name


def test_resolve_within_returns_contained_path(tmp_path: Path) -> None:
    target = tmp_path / "backup.vma.zst"
    target.write_bytes(b"data")
    assert resolve_within(tmp_path, "backup.vma.zst") == target.resolve()


def test_resolve_within_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        resolve_within(tmp_path, "../outside.vma.zst")


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside the root pointing outside it must not be followed."""
    outside = tmp_path.parent / "outside-secret"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.vma.zst"
    secret.write_text("sensitive", encoding="utf-8")

    root = tmp_path / "dump"
    root.mkdir()
    (root / "innocent.vma.zst").symlink_to(secret)

    with pytest.raises(ValidationFailed, match="escapes the permitted root"):
        resolve_within(root, "innocent.vma.zst")


def test_resolve_within_rejects_the_root_itself(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        resolve_within(tmp_path, ".")


def test_assert_within_accepts_child_and_rejects_sibling(tmp_path: Path) -> None:
    root = tmp_path / "dump"
    root.mkdir()
    child = root / "file.vma.zst"
    child.write_bytes(b"x")
    assert assert_within(root, child) == child.resolve()

    sibling = tmp_path / "other.vma.zst"
    sibling.write_bytes(b"x")
    with pytest.raises(ValidationFailed):
        assert_within(root, sibling)
