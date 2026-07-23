"""Digest computation with a ``sha256sum``-compatible sidecar cache.

Hashing a 40 GiB artifact takes minutes, so it never happens inside a request. It runs once
at the end of a backup task, and the result is cached beside the artifact in the standard
``<hex>  <filename>`` format — readable by ``sha256sum -c`` without ProxSync involved.

**Two algorithms, for two different jobs.** SHA-256 is ProxSync's own integrity record. MD5
exists solely to compare against Google Drive, which stores an MD5 for every uploaded file
and will return it without a download; asking Drive for a SHA-256 would make rclone fetch the
whole artifact back just to hash it. Verification therefore compares the hash the remote
actually holds, and says so when a remote holds none.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.logging import logger

Algorithm = Literal["sha256", "md5"]

SIDECAR_SUFFIX = ".sha256"
_SUFFIXES: dict[Algorithm, str] = {"sha256": ".sha256", "md5": ".md5"}
_DIGEST_LENGTHS: dict[Algorithm, int] = {"sha256": 64, "md5": 32}
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class ChecksumResult:
    hex_digest: str
    from_cache: bool
    algorithm: Algorithm = "sha256"


def sidecar_path(artifact_path: Path, algorithm: Algorithm = "sha256") -> Path:
    return artifact_path.with_name(artifact_path.name + _SUFFIXES[algorithm])


def read_cached(artifact_path: Path, algorithm: Algorithm = "sha256") -> str | None:
    """Return the cached digest when the sidecar is present and newer than the artifact."""
    sidecar = sidecar_path(artifact_path, algorithm)
    try:
        if not sidecar.is_file() or sidecar.stat().st_mtime < artifact_path.stat().st_mtime:
            return None
        content = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    digest = content.split(maxsplit=1)[0] if content else ""
    if len(digest) != _DIGEST_LENGTHS[algorithm] or not set(digest.lower()) <= _HEX:
        return None
    return digest.lower()


def _write_cache(artifact_path: Path, digest: str, algorithm: Algorithm) -> None:
    sidecar = sidecar_path(artifact_path, algorithm)
    temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
    try:
        temporary.write_text(f"{digest}  {artifact_path.name}\n", encoding="utf-8")
        temporary.replace(sidecar)
    except OSError:
        logger.warning("checksum_cache_write_failed", path=str(sidecar), exc_info=True)
        temporary.unlink(missing_ok=True)


def _hash_file(path: Path, chunk_bytes: int, algorithm: Algorithm) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


async def compute_digest(
    artifact_path: Path,
    *,
    algorithm: Algorithm = "sha256",
    chunk_bytes: int = 4 * 1024 * 1024,
    use_cache: bool = True,
) -> ChecksumResult:
    """Hash ``artifact_path`` off the event loop, caching the result in a sidecar."""
    if use_cache:
        cached = read_cached(artifact_path, algorithm)
        if cached is not None:
            return ChecksumResult(hex_digest=cached, from_cache=True, algorithm=algorithm)

    digest = await asyncio.to_thread(_hash_file, artifact_path, chunk_bytes, algorithm)
    await asyncio.to_thread(_write_cache, artifact_path, digest, algorithm)
    return ChecksumResult(hex_digest=digest, from_cache=False, algorithm=algorithm)


async def compute_sha256(
    artifact_path: Path, *, chunk_bytes: int = 4 * 1024 * 1024, use_cache: bool = True
) -> ChecksumResult:
    return await compute_digest(
        artifact_path, algorithm="sha256", chunk_bytes=chunk_bytes, use_cache=use_cache
    )


async def compute_md5(
    artifact_path: Path, *, chunk_bytes: int = 4 * 1024 * 1024, use_cache: bool = True
) -> ChecksumResult:
    return await compute_digest(
        artifact_path, algorithm="md5", chunk_bytes=chunk_bytes, use_cache=use_cache
    )
