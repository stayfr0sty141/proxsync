"""argv construction and command-output parsing.

These tests are the contract with Proxmox: getting an argument order or flag name wrong here
is the difference between a restore and a disaster.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ValidationFailed
from app.executors.base import validate_argv
from app.executors.pvesm import build_status_argv as pvesm_status_argv
from app.executors.pvesm import parse_status
from app.executors.restore import (
    RestoreCommand,
    build_start_argv,
    build_status_argv,
    build_stop_argv,
    parse_status_output,
)
from app.executors.vzdump import VzdumpCommand
from app.schemas.enums import BackupMode, Compression, GuestType

VZDUMP = Path("/usr/bin/vzdump")
QMRESTORE = Path("/usr/sbin/qmrestore")
QM = Path("/usr/sbin/qm")
PCT = Path("/usr/sbin/pct")
PVESM = Path("/usr/sbin/pvesm")


class TestVzdumpArgv:
    def test_minimal_snapshot_backup(self) -> None:
        argv = VzdumpCommand(
            vmid=101,
            mode=BackupMode.SNAPSHOT,
            compression=Compression.ZSTD,
            storage="backup-hdd",
        ).build(VZDUMP)
        assert argv == [
            "/usr/bin/vzdump",
            "101",
            "--mode",
            "snapshot",
            "--compress",
            "zstd",
            "--storage",
            "backup-hdd",
        ]

    def test_all_options(self) -> None:
        argv = VzdumpCommand(
            vmid=104,
            mode=BackupMode.STOP,
            compression=Compression.ZSTD,
            storage="backup-hdd",
            zstd_threads=4,
            bwlimit_kbps=51200,
            tmpdir=Path("/mnt/backup-hdd/tmp"),
        ).build(VZDUMP)
        assert argv[:8] == [
            "/usr/bin/vzdump",
            "104",
            "--mode",
            "stop",
            "--compress",
            "zstd",
            "--storage",
            "backup-hdd",
        ]
        assert argv[8:] == [
            "--zstd",
            "4",
            "--bwlimit",
            "51200",
            "--tmpdir",
            "/mnt/backup-hdd/tmp",
        ]

    def test_no_compression_uses_vzdump_zero(self) -> None:
        argv = VzdumpCommand(
            vmid=101,
            mode=BackupMode.SUSPEND,
            compression=Compression.NONE,
            storage="backup-hdd",
        ).build(VZDUMP)
        assert argv[argv.index("--compress") + 1] == "0"

    def test_zstd_threads_ignored_for_other_compressors(self) -> None:
        argv = VzdumpCommand(
            vmid=101,
            mode=BackupMode.SNAPSHOT,
            compression=Compression.GZIP,
            storage="backup-hdd",
            zstd_threads=8,
        ).build(VZDUMP)
        assert "--zstd" not in argv

    def test_zero_bandwidth_limit_is_omitted(self) -> None:
        argv = VzdumpCommand(
            vmid=101,
            mode=BackupMode.SNAPSHOT,
            compression=Compression.ZSTD,
            storage="backup-hdd",
            bwlimit_kbps=None,
        ).build(VZDUMP)
        assert "--bwlimit" not in argv

    def test_every_argument_is_a_string(self) -> None:
        argv = VzdumpCommand(
            vmid=101,
            mode=BackupMode.SNAPSHOT,
            compression=Compression.ZSTD,
            storage="backup-hdd",
            zstd_threads=0,
        ).build(VZDUMP)
        assert all(isinstance(item, str) for item in argv)


class TestRestoreArgv:
    ARCHIVE = Path("/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst")

    def test_qmrestore_takes_archive_before_vmid(self) -> None:
        argv = RestoreCommand(
            archive_path=self.ARCHIVE, target_vmid=151, storage="local-lvm"
        ).build_vm(QMRESTORE)
        assert argv == [
            "/usr/sbin/qmrestore",
            str(self.ARCHIVE),
            "151",
            "--storage",
            "local-lvm",
        ]

    def test_pct_restore_takes_vmid_before_archive(self) -> None:
        argv = RestoreCommand(
            archive_path=self.ARCHIVE, target_vmid=151, storage="local-lvm"
        ).build_lxc(PCT)
        assert argv == [
            "/usr/sbin/pct",
            "restore",
            "151",
            str(self.ARCHIVE),
            "--storage",
            "local-lvm",
        ]

    def test_overwrite_adds_force(self) -> None:
        argv = RestoreCommand(
            archive_path=self.ARCHIVE, target_vmid=101, storage="local-lvm", overwrite=True
        ).build_vm(QMRESTORE)
        assert argv[-2:] == ["--force", "1"]

    def test_force_is_absent_by_default(self) -> None:
        argv = RestoreCommand(
            archive_path=self.ARCHIVE, target_vmid=151, storage="local-lvm"
        ).build_vm(QMRESTORE)
        assert "--force" not in argv

    def test_lxc_unprivileged_flag(self) -> None:
        argv = RestoreCommand(
            archive_path=self.ARCHIVE,
            target_vmid=151,
            storage="local-lvm",
            unprivileged=True,
        ).build_lxc(PCT)
        assert argv[-2:] == ["--unprivileged", "1"]

    def test_lifecycle_argv(self) -> None:
        assert build_status_argv(QM, GuestType.VM, 101) == ["/usr/sbin/qm", "status", "101"]
        assert build_stop_argv(PCT, 104) == ["/usr/sbin/pct", "stop", "104"]
        assert build_start_argv(QM, 151) == ["/usr/sbin/qm", "start", "151"]


class TestStatusParsing:
    def test_running(self) -> None:
        assert parse_status_output("status: running\n") == "running"

    def test_stopped_with_extra_lines(self) -> None:
        assert parse_status_output("cpus: 4\nstatus: stopped\nmem: 0\n") == "stopped"

    def test_unrecognised_output(self) -> None:
        assert parse_status_output("something went wrong") == "unknown"


class TestPvesmParsing:
    OUTPUT = """\
Name             Type     Status           Total            Used       Available        %
backup-hdd        dir     active       488384352       312516608       151011744   64.00%
local             dir     active        30832548         3288400        25955116   10.67%
broken            nfs   inactive               0               0               0    0.00%
"""

    def test_argv(self) -> None:
        assert pvesm_status_argv(PVESM) == ["/usr/sbin/pvesm", "status"]

    def test_parses_all_rows(self) -> None:
        entries = parse_status(self.OUTPUT)
        assert [entry.name for entry in entries] == ["backup-hdd", "local", "broken"]

    def test_converts_kib_to_bytes(self) -> None:
        backup = parse_status(self.OUTPUT)[0]
        assert backup.total_bytes == 488384352 * 1024
        assert backup.used_bytes == 312516608 * 1024
        assert backup.available_bytes == 151011744 * 1024
        # pvesm rounds its own % column to 64.00; the exact ratio is 63.99.
        assert backup.used_percent == 63.99

    def test_flags_inactive_storage(self) -> None:
        broken = parse_status(self.OUTPUT)[2]
        assert broken.active is False
        assert broken.used_percent == 0.0

    def test_ignores_garbage_lines(self) -> None:
        assert parse_status("total nonsense\n\n") == []


class TestArgvValidation:
    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationFailed, match="Empty command"):
            validate_argv([])

    def test_rejects_relative_executable(self) -> None:
        with pytest.raises(ValidationFailed, match="absolute path"):
            validate_argv(["vzdump", "101"])

    def test_rejects_null_bytes(self) -> None:
        with pytest.raises(ValidationFailed, match="null byte"):
            validate_argv(["/usr/bin/vzdump", "101\x00; rm -rf /"])

    def test_rejects_non_string_arguments(self) -> None:
        with pytest.raises(ValidationFailed, match="not a string"):
            validate_argv(["/usr/bin/vzdump", 101])  # type: ignore[list-item]

    def test_accepts_valid_command(self) -> None:
        assert validate_argv(["/usr/bin/vzdump", "101"]) == ["/usr/bin/vzdump", "101"]


class TestProcessRunnerSafety:
    @pytest.mark.asyncio
    async def test_run_capture_has_distinct_pgid(self) -> None:
        import os

        from app.executors.base import ProcessRunner

        runner = ProcessRunner()
        # Python prints its own pgid via os.getpgid(0)
        code, out = await runner.run_capture(
            ["/usr/bin/python3", "-c", "import os; print(os.getpgid(0))"],
            timeout_seconds=5,
        )
        assert code == 0
        child_pgid = int(out.strip())
        agent_pgid = os.getpgid(0)
        assert child_pgid != agent_pgid

    @pytest.mark.asyncio
    async def test_run_capture_timeout_kills_descendants_and_keeps_agent_alive(self) -> None:
        from app.core.errors import ExecutionFailed
        from app.executors.base import ProcessRunner

        runner = ProcessRunner()
        # Spawn a python process that spawns a sleep grandchild and waits
        cmd = [
            "/usr/bin/python3",
            "-c",
            "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)",
        ]
        with pytest.raises(ExecutionFailed, match="timed out"):
            await runner.run_capture(cmd, timeout_seconds=1)
