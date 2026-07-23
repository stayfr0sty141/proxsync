"""rclone remote and path validation.

This is a security boundary: everything here ends up as an argv element in a command that can
read and write files. The tests are written as attacks.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailed
from app.validators.remotes import (
    build_remote_spec,
    validate_remote_name,
    validate_remote_path,
)


class TestRemoteName:
    @pytest.mark.parametrize("name", ["gdrive", "g-drive", "drive_2", "a.b", "G1", "x" * 64])
    def test_accepts_well_formed_names(self, name: str) -> None:
        assert validate_remote_name(name) == name

    def test_trailing_colon_is_tolerated(self) -> None:
        """rclone writes remotes as ``gdrive:``; a caller pasting that must not be punished."""
        assert validate_remote_name("gdrive:") == "gdrive"

    def test_surrounding_whitespace_is_trimmed_but_embedded_whitespace_is_not(self) -> None:
        """Trimming a pasted value is normalisation; accepting a name with a newline *in* it
        would put a second line into an argv element."""
        assert validate_remote_name("  gdrive\n") == "gdrive"

        with pytest.raises(ValidationFailed):
            validate_remote_name("gd\nrive")

    @pytest.mark.parametrize(
        "name",
        [
            "",
            " ",
            "-gdrive",  # rclone would read a leading dash as a flag
            ".hidden",
            "gdrive/../etc",
            "gdrive space",
            "gdrive;rm -rf /",
            "gdrive\x00",
            "gd\nrive",
            "gdrive\nrm -rf /",
            "$(whoami)",
            "`id`",
            "a" * 65,
        ],
    )
    def test_rejects_hostile_names(self, name: str) -> None:
        with pytest.raises(ValidationFailed):
            validate_remote_name(name)

    def test_enforces_the_allow_list(self) -> None:
        assert validate_remote_name("gdrive", allowed=["gdrive", "backblaze"]) == "gdrive"

        with pytest.raises(ValidationFailed) as excinfo:
            validate_remote_name("local", allowed=["gdrive"])
        assert "allow-list" in excinfo.value.detail

    def test_an_empty_allow_list_permits_any_well_formed_name(self) -> None:
        assert validate_remote_name("anything", allowed=[]) == "anything"

    def test_a_local_remote_can_be_excluded(self) -> None:
        """The point of the allow-list: `rclone copyto local:/etc/shadow` must be impossible."""
        with pytest.raises(ValidationFailed):
            validate_remote_name("local", allowed=["gdrive"])


class TestRemotePath:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("", ""),
            ("/", ""),
            ("proxsync", "proxsync"),
            ("proxsync/dump", "proxsync/dump"),
            ("/proxsync/dump/", "proxsync/dump"),
            ("  proxsync/dump  ", "proxsync/dump"),
        ],
    )
    def test_normalises_valid_paths(self, path: str, expected: str) -> None:
        assert validate_remote_path(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "../etc",
            "proxsync/../../etc",
            "proxsync/..",
            "a//b",
            "-x",
            "other:path",
            "with\x00null",
            "with\nnewline",
            "with\ttab",
        ],
    )
    def test_rejects_traversal_and_control_characters(self, path: str) -> None:
        with pytest.raises(ValidationFailed):
            validate_remote_path(path)

    @pytest.mark.parametrize("path", ["dump/*", "dump/?", "dump/[a-z]", "dump/{a,b}"])
    def test_rejects_rclone_filter_metacharacters(self, path: str) -> None:
        """`*` in a path that later reaches a filter would widen what a command touches."""
        with pytest.raises(ValidationFailed) as excinfo:
            validate_remote_path(path)
        assert "filter pattern" in excinfo.value.detail

    def test_rejects_an_over_long_path(self) -> None:
        with pytest.raises(ValidationFailed):
            validate_remote_path("a" * 1025)


class TestRemoteSpec:
    def test_composes_remote_path_and_filename(self) -> None:
        spec = build_remote_spec("gdrive", "proxsync/dump", "vzdump-qemu-101.vma.zst")
        assert spec == "gdrive:proxsync/dump/vzdump-qemu-101.vma.zst"

    def test_omits_empty_parts(self) -> None:
        assert build_remote_spec("gdrive") == "gdrive:"
        assert build_remote_spec("gdrive", "", "file.zst") == "gdrive:file.zst"
        assert build_remote_spec("gdrive", "dump") == "gdrive:dump"
