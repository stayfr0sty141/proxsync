"""Path containment.

Every filesystem name that arrives from the network passes through here before it is used in
an argv list. The rules are deliberately strict: the agent only ever needs flat basenames
inside a single configured root, so anything else is rejected rather than sanitised.
"""

from __future__ import annotations

from pathlib import Path

from app.core.errors import ValidationFailed

_MAX_NAME_LENGTH = 255
_FORBIDDEN_NAMES = {"", ".", ".."}


def validate_basename(name: str) -> str:
    """Return ``name`` if it is a safe, flat filename.

    Rejected: path separators, traversal, NUL and control bytes, leading ``-`` (which a
    command would parse as an option), whitespace-only names, and over-long names.
    """
    if not isinstance(name, str):  # pragma: no cover - defensive, Pydantic types the input
        raise ValidationFailed("Filename must be a string")

    if name in _FORBIDDEN_NAMES or not name.strip():
        raise ValidationFailed("Filename is empty or refers to a directory entry")

    if len(name) > _MAX_NAME_LENGTH:
        raise ValidationFailed(f"Filename exceeds {_MAX_NAME_LENGTH} characters")

    if "/" in name or "\\" in name:
        raise ValidationFailed("Filename must not contain a path separator")

    if "\x00" in name:
        raise ValidationFailed("Filename must not contain a null byte")

    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise ValidationFailed("Filename must not contain control characters")

    if name.startswith("-"):
        raise ValidationFailed("Filename must not start with '-'")

    if name != Path(name).name:
        raise ValidationFailed("Filename must be a plain basename")

    return name


def resolve_within(root: Path, name: str) -> Path:
    """Join ``name`` to ``root`` and prove the result stays inside it.

    Resolution follows symlinks, so a symlink in the dump directory pointing at ``/etc`` is
    caught by the containment check rather than followed.
    """
    validate_basename(name)
    resolved_root = root.resolve()
    candidate = (resolved_root / name).resolve()

    if candidate == resolved_root or not candidate.is_relative_to(resolved_root):
        raise ValidationFailed(f"Resolved path escapes the permitted root: {name}")

    return candidate


def assert_within(root: Path, candidate: Path) -> Path:
    """Containment check for a path the agent built itself (sidecars, temp files)."""
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValidationFailed(f"Path escapes the permitted root: {candidate}")
    return resolved
