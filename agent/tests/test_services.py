"""Full task lifecycle at the service layer, where execution can be awaited deterministically."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from app.api.deps import Container
from app.core.config import AgentSettings
from app.core.errors import ConcurrencyConflict
from app.executors.base import ProcessResult
from app.schemas.enums import TaskState
from app.schemas.requests import BackupStartRequest, RestoreLxcRequest, RestoreVmRequest
from tests.conftest import (
    LXC_BACKUP_OUTPUT,
    QEMU_BACKUP_OUTPUT,
    RESTORE_OUTPUT,
    ArtifactFactory,
    FakeProcessRunner,
)

ARCHIVE = "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"
LXC_ARCHIVE = "vzdump-lxc-104-2026_07_19-01_07_22.tar.zst"


def backup_request(**overrides: object) -> BackupStartRequest:
    payload = {
        "vmid": 101,
        "guest_type": "vm",
        "mode": "snapshot",
        "compression": "zstd",
        "storage": "backup-hdd",
        **overrides,
    }
    return BackupStartRequest.model_validate(payload)


class TestBackupLifecycle:
    async def test_successful_backup_records_artifact_metadata(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        archive_path = settings.dump_root / ARCHIVE
        runner.creates_archive = archive_path
        runner.output = QEMU_BACKUP_OUTPUT.format(archive=archive_path)

        task = await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert finished.exit_code == 0
        assert finished.result["filename"] == ARCHIVE
        assert finished.result["size_bytes"] == archive_path.stat().st_size
        assert (
            finished.result["checksum_sha256"]
            == hashlib.sha256(archive_path.read_bytes()).hexdigest()
        )
        assert "warnings" not in finished.result
        assert finished.progress.percent == 100.0

    async def test_checksum_sidecar_is_written(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        archive_path = settings.dump_root / ARCHIVE
        runner.creates_archive = archive_path
        runner.output = QEMU_BACKUP_OUTPUT.format(archive=archive_path)

        await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        sidecar = settings.dump_root / f"{ARCHIVE}.sha256"
        assert sidecar.is_file()
        digest, name = sidecar.read_text(encoding="utf-8").split()
        assert name == ARCHIVE
        assert digest == hashlib.sha256(archive_path.read_bytes()).hexdigest()

    async def test_notes_sidecar_is_written(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        archive_path = settings.dump_root / ARCHIVE
        runner.creates_archive = archive_path
        runner.output = QEMU_BACKUP_OUTPUT.format(archive=archive_path)

        await container.backup_service.start(backup_request(notes="proxsync run 812"))
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        notes = settings.dump_root / f"{ARCHIVE}.notes"
        assert notes.read_text(encoding="utf-8").strip() == "proxsync run 812"

    async def test_lxc_backup_records_bytes_without_a_percentage(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        archive_path = settings.dump_root / LXC_ARCHIVE
        runner.creates_archive = archive_path
        runner.output = LXC_BACKUP_OUTPUT.format(archive=archive_path)

        task = await container.backup_service.start(backup_request(vmid=104, guest_type="lxc"))
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert finished.progress.bytes_done == 2274918400
        assert finished.progress.percent is None  # tar backups report no percentage

    async def test_failed_backup_captures_the_error_line(
        self, container: Container, runner: FakeProcessRunner
    ) -> None:
        runner.exit_code = 1
        runner.output = (
            "INFO: Starting Backup of VM 101 (qemu)\n"
            "ERROR: Backup of VM 101 failed - no such storage 'backup-hdd'\n"
        )

        task = await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.FAILED
        assert finished.exit_code == 1
        assert finished.error == "Backup of VM 101 failed - no such storage 'backup-hdd'"

    async def test_failure_without_an_error_line_falls_back_to_the_tail(
        self, container: Container, runner: FakeProcessRunner
    ) -> None:
        runner.exit_code = 2
        runner.output = "something unexpected\nand then it stopped\n"

        task = await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.FAILED
        assert "and then it stopped" in (finished.error or "")

    async def test_archive_written_outside_the_dump_root_is_flagged(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        outside = settings.dump_root.parent / ARCHIVE
        runner.creates_archive = outside
        runner.output = QEMU_BACKUP_OUTPUT.format(archive=outside)

        task = await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert any("outside" in warning for warning in finished.result["warnings"])
        assert "checksum_sha256" not in finished.result

    async def test_success_without_an_archive_path_is_flagged(
        self, container: Container, runner: FakeProcessRunner
    ) -> None:
        runner.output = "INFO: Backup job finished successfully\n"

        task = await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert "no archive path" in finished.result["warnings"][0]

    async def test_slot_is_released_after_completion(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        archive_path = settings.dump_root / ARCHIVE
        runner.creates_archive = archive_path
        runner.output = QEMU_BACKUP_OUTPUT.format(archive=archive_path)

        await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        # A second backup must be accepted once the first has finished.
        await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        backup_slot = next(slot for slot in container.slots.state() if slot.name == "backup")
        assert backup_slot.in_use == 0

    async def test_slot_is_released_after_failure(
        self, container: Container, runner: FakeProcessRunner
    ) -> None:
        runner.exit_code = 1
        await container.backup_service.start(backup_request())
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        backup_slot = next(slot for slot in container.slots.state() if slot.name == "backup")
        assert backup_slot.in_use == 0

    async def test_concurrent_backup_is_refused(self, container: Container) -> None:
        await container.backup_service.start(backup_request())
        second_request = backup_request(vmid=104, guest_type="lxc")
        with pytest.raises(ConcurrencyConflict):
            await container.backup_service.start(second_request)
        async with asyncio.timeout(5):
            await container.backup_service.drain()

    async def test_vzdump_receives_the_expected_argv(
        self, container: Container, settings: AgentSettings, runner: FakeProcessRunner
    ) -> None:
        await container.backup_service.start(
            backup_request(mode="stop", compression="zstd", zstd_threads=2, bwlimit_kbps=1024)
        )
        async with asyncio.timeout(5):
            await container.backup_service.drain()

        assert runner.logged_calls[0] == [
            str(settings.vzdump_bin),
            "101",
            "--mode",
            "stop",
            "--compress",
            "zstd",
            "--storage",
            "backup-hdd",
            "--zstd",
            "2",
            "--bwlimit",
            "1024",
            "--tmpdir",
            str(settings.temp_dir),
        ]


class TestRestoreLifecycle:
    async def test_successful_vm_restore(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.output = RESTORE_OUTPUT

        request = RestoreVmRequest.model_validate(
            {"archive": ARCHIVE, "target_vmid": 151, "storage": "local-lvm"}
        )
        task = await container.restore_service.restore_vm(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert finished.result["target_vmid"] == 151
        assert finished.result["started"] is False
        assert runner.logged_calls[0] == [
            str(settings.qmrestore_bin),
            str(settings.dump_root / ARCHIVE),
            "151",
            "--storage",
            "local-lvm",
        ]

    async def test_start_after_restore_issues_qm_start(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE)
        request = RestoreVmRequest.model_validate(
            {
                "archive": ARCHIVE,
                "target_vmid": 151,
                "storage": "local-lvm",
                "start_after": True,
            }
        )
        task = await container.restore_service.restore_vm(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        assert container.registry.get(task.id).result["started"] is True
        assert runner.logged_calls[-1] == [str(settings.qm_bin), "start", "151"]

    async def test_force_stop_stops_before_restoring(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.capture_output = "status: running\n"

        request = RestoreVmRequest.model_validate(
            {
                "archive": ARCHIVE,
                "target_vmid": 101,
                "storage": "local-lvm",
                "overwrite": True,
                "force_stop": True,
            }
        )
        task = await container.restore_service.restore_vm(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        assert container.registry.get(task.id).state is TaskState.SUCCESS
        assert runner.logged_calls[0] == [str(settings.qm_bin), "stop", "101"]
        assert runner.logged_calls[1][0] == str(settings.qmrestore_bin)
        assert "--force" in runner.logged_calls[1]

    async def test_lxc_restore_argv_order(
        self,
        container: Container,
        settings: AgentSettings,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(LXC_ARCHIVE)
        request = RestoreLxcRequest.model_validate(
            {"archive": LXC_ARCHIVE, "target_vmid": 154, "storage": "local-lvm"}
        )
        await container.restore_service.restore_lxc(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        assert runner.logged_calls[0] == [
            str(settings.pct_bin),
            "restore",
            "154",
            str(settings.dump_root / LXC_ARCHIVE),
            "--storage",
            "local-lvm",
        ]

    async def test_failed_restore_is_recorded(
        self, container: Container, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.exit_code = 1
        runner.output = "ERROR: unable to restore - storage 'local-lvm' is full\n"

        request = RestoreVmRequest.model_validate(
            {"archive": ARCHIVE, "target_vmid": 151, "storage": "local-lvm"}
        )
        task = await container.restore_service.restore_vm(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.FAILED
        assert "storage 'local-lvm' is full" in (finished.error or "")

    async def test_restore_slot_is_released(
        self, container: Container, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE)
        request = RestoreVmRequest.model_validate(
            {"archive": ARCHIVE, "target_vmid": 151, "storage": "local-lvm"}
        )
        await container.restore_service.restore_vm(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        restore_slot = next(slot for slot in container.slots.state() if slot.name == "restore")
        assert restore_slot.in_use == 0

    async def test_failing_to_start_after_restore_is_a_warning_not_a_failure(
        self, container: Container, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.exit_code = 0

        request = RestoreVmRequest.model_validate(
            {
                "archive": ARCHIVE,
                "target_vmid": 151,
                "storage": "local-lvm",
                "start_after": True,
            }
        )

        # Make only the trailing `qm start` fail.
        original = runner.run_logged
        call_count = {"n": 0}

        async def flaky(*args: Any, **kwargs: Any) -> ProcessResult:
            call_count["n"] += 1
            if call_count["n"] == 2:
                runner.exit_code = 1
            return await original(*args, **kwargs)

        runner.run_logged = flaky  # type: ignore[method-assign]

        task = await container.restore_service.restore_vm(request)
        async with asyncio.timeout(5):
            await container.restore_service.drain()

        finished = container.registry.get(task.id)
        assert finished.state is TaskState.SUCCESS
        assert finished.result["started"] is False
        assert "failed to start" in finished.result["warnings"][0]
