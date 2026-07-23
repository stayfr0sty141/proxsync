"""Sync task lifecycle at the service layer, where execution can be awaited deterministically."""

from __future__ import annotations

import asyncio
import hashlib
import json

from app.api.deps import Container
from app.core.config import AgentSettings
from app.schemas.enums import TaskKind, TaskState
from app.schemas.requests import SyncDownloadRequest, SyncUploadRequest, SyncVerifyRequest
from tests.conftest import RCLONE_UPLOAD_OUTPUT, ArtifactFactory, FakeProcessRunner

ARCHIVE = "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"


def upload_request(**overrides: object) -> SyncUploadRequest:
    payload = {
        "filename": ARCHIVE,
        "remote": "gdrive",
        "remote_path": "proxsync/dump",
        **overrides,
    }
    return SyncUploadRequest.model_validate(payload)


def rclone_calls(runner: FakeProcessRunner) -> list[list[str]]:
    return [call for call in runner.calls if call[0].endswith("rclone")]


def lsjson_payload(*, size: int, md5: str | None) -> str:
    entry: dict[str, object] = {"Name": ARCHIVE, "Path": ARCHIVE, "Size": size, "IsDir": False}
    if md5 is not None:
        entry["Hashes"] = {"md5": md5}
    return json.dumps([entry])


class TestUploadLifecycle:
    async def test_a_successful_upload_records_its_outcome(
        self,
        container: Container,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE, size=4096)
        runner.output = RCLONE_UPLOAD_OUTPUT

        task = container.sync_service.upload(upload_request())
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        finished = container.registry.get(task.id)
        assert finished.kind is TaskKind.UPLOAD
        assert finished.state is TaskState.SUCCESS
        assert finished.meta["filename"] == ARCHIVE
        assert finished.meta["remote"] == "gdrive"
        # Progress came from the rclone stats lines, not from a guess.
        assert finished.progress.percent == 100.0

    async def test_argv_names_the_source_and_destination_exactly(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.output = RCLONE_UPLOAD_OUTPUT

        container.sync_service.upload(upload_request())
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        [argv] = rclone_calls(runner)
        assert argv[0] == str(settings.rclone_bin)
        assert "copyto" in argv
        assert argv[-2] == str(settings.dump_root / ARCHIVE)
        assert argv[-1] == f"gdrive:proxsync/dump/{ARCHIVE}"
        assert all(isinstance(item, str) for item in argv)

    async def test_bandwidth_limit_reaches_rclone(
        self,
        container: Container,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.output = RCLONE_UPLOAD_OUTPUT

        container.sync_service.upload(upload_request(bwlimit_kbps=2048))
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        [argv] = rclone_calls(runner)
        assert argv[argv.index("--bwlimit") + 1] == "2048k"

    async def test_a_failing_transfer_reports_rclones_own_error(
        self,
        container: Container,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.exit_code = 1
        runner.output = (
            "2026/07/26 01:15:03 ERROR : a.vma.zst: Failed to copy: "
            "googleapi: Error 403: The user's Drive storage quota has been exceeded"
        )

        task = container.sync_service.upload(upload_request())
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.FAILED
        assert finished.error is not None
        assert "quota has been exceeded" in finished.error

    async def test_the_slot_is_released_after_a_failure(
        self,
        container: Container,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        """A failed upload that held the slot would block every later transfer."""
        artifact_factory(ARCHIVE)
        runner.exit_code = 1

        container.sync_service.upload(upload_request())
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        slot = next(slot for slot in container.slots.state() if slot.name == "sync")
        assert slot.in_use == 0


class TestVerifyAfterUpload:
    async def test_a_verified_upload_succeeds(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        path = artifact_factory(ARCHIVE, size=2048)
        digest = hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - Drive publishes MD5
        runner.output = RCLONE_UPLOAD_OUTPUT
        runner.capture_output = lsjson_payload(size=2048, md5=digest)

        task = container.sync_service.upload(upload_request(verify_after=True))
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert finished.result["verification"]["outcome"] == "match"

    async def test_an_upload_that_fails_verification_is_a_failed_upload(
        self,
        container: Container,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        """rclone exiting 0 with a corrupt result is not a success with a footnote."""
        artifact_factory(ARCHIVE, size=2048)
        runner.output = RCLONE_UPLOAD_OUTPUT
        runner.capture_output = lsjson_payload(size=2048, md5="0" * 32)

        task = container.sync_service.upload(upload_request(verify_after=True))
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.FAILED
        assert finished.error is not None
        assert "hash_mismatch" in finished.error


class TestDownloadLifecycle:
    async def test_argv_names_the_remote_source_and_local_destination(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        runner.output = RCLONE_UPLOAD_OUTPUT
        request = SyncDownloadRequest.model_validate(
            {"filename": ARCHIVE, "remote": "gdrive", "remote_path": "proxsync/dump"}
        )

        container.sync_service.download(request)
        async with asyncio.timeout(5):
            await container.sync_service.drain()

        [argv] = rclone_calls(runner)
        assert argv[-2] == f"gdrive:proxsync/dump/{ARCHIVE}"
        assert argv[-1] == str(settings.dump_root / ARCHIVE)


class TestVerification:
    async def test_the_local_digest_is_cached_in_a_sidecar(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        """Re-verifying a 40 GiB artifact must not re-hash it every time."""
        path = artifact_factory(ARCHIVE, size=2048)
        digest = hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324
        runner.capture_output = lsjson_payload(size=2048, md5=digest)
        request = SyncVerifyRequest.model_validate(
            {"filename": ARCHIVE, "remote": "gdrive", "remote_path": "proxsync/dump"}
        )

        first = await container.sync_service.verify(request)
        sidecar = settings.dump_root / f"{ARCHIVE}.md5"
        assert first.verified
        assert sidecar.is_file()
        assert sidecar.read_text(encoding="utf-8").split()[0] == digest

        second = await container.sync_service.verify(request)
        assert second.verified
