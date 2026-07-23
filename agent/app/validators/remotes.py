"""rclone remote and remote-path validation.

An rclone argument like ``gdrive:proxsync/dump/file.vma.zst`` is a *command argument*, not a
filesystem path, and rclone gives several of its characters meaning. Everything that reaches
an argv list is therefore validated here first:

* A remote **name** is checked against the configured allow-list, so a request can never name
  a remote the operator did not set up — including a local one. ``rclone copyto x /etc/shadow``
  would otherwise be a file read primitive.
* A remote **path** may not contain ``..``, may not be absolute, and may not contain the glob
  metacharacters rclone's filter language uses. `*`, `?`, `[` and `{` in a path that later
  reaches ``--include`` would silently widen what a command touches.
* Nothing may begin with ``-``: rclone would parse it as a flag.

There is no sanitising. A value that is not already safe is rejected.
"""

from __future__ import annotations

import re

from app.core.errors import ValidationFailed

REMOTE_NAME_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
r"""rclone's own rule, minus a leading '-' or '.' which would read as a flag or a hidden name.

``\A``/``\Z`` rather than ``^``/``$``: in Python ``$`` also matches *before* a trailing
newline, so ``"gdrive\n"`` would pass a ``$``-anchored check and reach an argv list."""

MAX_REMOTE_PATH_LENGTH = 1024
_GLOB_CHARACTERS = "*?[]{}"


def validate_remote_name(name: str, *, allowed: list[str] | None = None) -> str:
    """Return the remote name, or raise. ``allowed`` empty/None means "any well-formed name"."""
    cleaned = name.strip().rstrip(":")

    if not REMOTE_NAME_PATTERN.match(cleaned):
        raise ValidationFailed(
            f"'{name}' is not a valid rclone remote name; expected letters, digits, "
            "'.', '_' or '-', starting with a letter or digit"
        )

    if allowed and cleaned not in allowed:
        raise ValidationFailed(
            f"Remote '{cleaned}' is not in the agent's allow-list. "
            f"Permitted remotes: {', '.join(sorted(allowed))}"
        )

    return cleaned


def validate_remote_path(path: str) -> str:
    """Return a normalised remote directory path (no leading or trailing slash)."""
    if len(path) > MAX_REMOTE_PATH_LENGTH:
        raise ValidationFailed(f"Remote path exceeds {MAX_REMOTE_PATH_LENGTH} characters")

    if "\x00" in path:
        raise ValidationFailed("Remote path must not contain a null byte")

    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise ValidationFailed("Remote path must not contain control characters")

    cleaned = path.strip().strip("/")
    if not cleaned:
        return ""

    if cleaned.startswith("-"):
        raise ValidationFailed("Remote path must not start with '-'")

    if ":" in cleaned:
        raise ValidationFailed("Remote path must not contain ':'; the remote is named separately")

    for character in _GLOB_CHARACTERS:
        if character in cleaned:
            raise ValidationFailed(
                f"Remote path must not contain '{character}': rclone reads it as a filter "
                "pattern, which would change which files a command touches"
            )

    for segment in cleaned.split("/"):
        if segment in {"", ".", ".."}:
            raise ValidationFailed(
                f"Remote path segment '{segment}' is not allowed; "
                "'..' and empty segments are rejected"
            )

    return cleaned


def build_remote_spec(remote: str, path: str = "", filename: str = "") -> str:
    """Compose ``remote:path/filename`` from already-validated parts."""
    tail = "/".join(part for part in (path, filename) if part)
    return f"{remote}:{tail}"
