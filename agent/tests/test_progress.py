"""Progress parsing against real vzdump / qmrestore output."""

from __future__ import annotations

from app.tasks.models import TaskProgress
from app.tasks.progress import RestoreProgressParser, VzdumpProgressParser


class TestVzdumpParser:
    def test_captures_archive_path(self) -> None:
        parser = VzdumpProgressParser()
        progress = TaskProgress()
        line = (
            "INFO: creating vzdump archive "
            "'/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst'"
        )
        assert parser.feed(line, progress)
        assert parser.archive_path == (
            "/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"
        )

    def test_captures_legacy_archive_line(self) -> None:
        parser = VzdumpProgressParser()
        assert parser.feed(
            "INFO: creating archive '/mnt/backup-hdd/dump/x.tar.zst'", TaskProgress()
        )
        assert parser.archive_path == "/mnt/backup-hdd/dump/x.tar.zst"

    def test_parses_qemu_status_line(self) -> None:
        parser = VzdumpProgressParser()
        progress = TaskProgress()
        line = (
            "INFO: status: 42% (12884901888/30601641984), sparse 0% (0), "
            "duration 312, read/write 41/41 MB/s"
        )
        assert parser.feed(line, progress)
        assert progress.percent == 42.0
        assert progress.bytes_done == 12884901888
        assert progress.bytes_total == 30601641984
        assert progress.rate_bps == 41 * 1000**2
        assert progress.eta_seconds is not None and progress.eta_seconds > 0

    def test_parses_human_readable_status(self) -> None:
        parser = VzdumpProgressParser()
        progress = TaskProgress()
        line = "INFO:  45% (1.2 GiB of 2.7 GiB) in 12s, read: 100 MiB/s, write: 90 MiB/s"
        assert parser.feed(line, progress)
        assert progress.percent == 45.0
        assert progress.bytes_done == int(1.2 * 1024**3)
        assert progress.bytes_total == int(2.7 * 1024**3)

    def test_parses_lxc_total_bytes_written(self) -> None:
        parser = VzdumpProgressParser()
        progress = TaskProgress()
        assert parser.feed("INFO: Total bytes written: 2274918400 (2.2GiB, 45MiB/s)", progress)
        assert progress.bytes_done == 2274918400

    def test_parses_archive_file_size(self) -> None:
        parser = VzdumpProgressParser()
        assert parser.feed("INFO: archive file size: 8.50GB", TaskProgress())
        assert parser.archive_size_bytes == int(8.5 * 1000**3)

    def test_captures_error_message(self) -> None:
        parser = VzdumpProgressParser()
        line = "ERROR: Backup of VM 103 failed - command 'qm guest cmd 103 fsfreeze-freeze' failed"
        assert parser.feed(line, TaskProgress())
        assert parser.error_message is not None
        assert "Backup of VM 103 failed" in parser.error_message

    def test_ignores_unrelated_lines(self) -> None:
        parser = VzdumpProgressParser()
        progress = TaskProgress()
        assert not parser.feed("INFO: starting new backup job", progress)
        assert progress.percent is None

    def test_full_session_reaches_hundred_percent(self) -> None:
        parser = VzdumpProgressParser()
        progress = TaskProgress()
        session = [
            "INFO: Starting Backup of VM 101 (qemu)",
            "INFO: creating vzdump archive '/mnt/backup-hdd/dump/vzdump-qemu-101-2026_07_19-01_00_04.vma.zst'",
            "INFO: status: 12% (3865470976/30601641984), sparse 0% (0), duration 33, read/write 117/117 MB/s",
            "INFO: status: 100% (30601641984/30601641984), sparse 4% (1288490188), duration 312, read/write 98/94 MB/s",
            "INFO: archive file size: 8.50GB",
            "INFO: Finished Backup of VM 101 (00:05:12)",
        ]
        for line in session:
            parser.feed(line, progress)

        assert progress.percent == 100.0
        assert progress.eta_seconds == 0
        assert parser.error_message is None
        assert parser.archive_path is not None


class TestRestoreParser:
    def test_parses_qmrestore_progress(self) -> None:
        parser = RestoreProgressParser()
        progress = TaskProgress()
        assert parser.feed("progress 25% (read 2147483648 bytes, duration 12 sec)", progress)
        assert progress.percent == 25.0
        assert progress.bytes_done == 2147483648
        assert progress.bytes_total == 2147483648 * 4

    def test_captures_error(self) -> None:
        parser = RestoreProgressParser()
        assert parser.feed(
            "ERROR: unable to restore - storage 'nope' does not exist", TaskProgress()
        )
        assert parser.error_message == "unable to restore - storage 'nope' does not exist"

    def test_ignores_noise(self) -> None:
        parser = RestoreProgressParser()
        assert not parser.feed("extracting archive", TaskProgress())
