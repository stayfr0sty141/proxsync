"""rclone argv construction and output parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import ExecutionFailed
from app.executors.rclone import (
    RcloneOptions,
    RcloneProgressParser,
    build_about_argv,
    build_copy_argv,
    build_delete_argv,
    build_lsjson_argv,
    parse_about,
    parse_eta,
    parse_lsjson,
)
from app.tasks.models import TaskProgress

RCLONE = Path("/usr/bin/rclone")


@pytest.fixture
def options() -> RcloneOptions:
    return RcloneOptions(rclone_bin=RCLONE, config_path=Path("/var/lib/proxsync-agent/rclone.conf"))


class TestArgv:
    def test_every_argument_is_a_separate_list_element(self, options: RcloneOptions) -> None:
        argv = build_copy_argv(
            options, source="/mnt/backup-hdd/dump/a.vma.zst", destination="gdrive:dump/a.vma.zst"
        )
        assert all(isinstance(item, str) for item in argv)
        assert argv[0] == str(RCLONE)
        assert argv[-2:] == ["/mnt/backup-hdd/dump/a.vma.zst", "gdrive:dump/a.vma.zst"]

    def test_uses_copyto_not_copy(self, options: RcloneOptions) -> None:
        """`copy` would create a *directory* named after the destination file."""
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:dump/a")
        assert "copyto" in argv
        assert "copy" not in argv

    def test_rclone_retries_are_disabled(self, options: RcloneOptions) -> None:
        """The dashboard owns retries; rclone retrying underneath would make its count a lie."""
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:a")
        assert argv[argv.index("--retries") + 1] == "1"

    def test_low_level_retries_stay_on(self, options: RcloneOptions) -> None:
        """A different thing: this is what lets one 40 GiB upload survive a dropped packet."""
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:a")
        assert argv[argv.index("--low-level-retries") + 1] == "3"

    def test_config_path_is_passed_explicitly(self, options: RcloneOptions) -> None:
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:a")
        assert argv[argv.index("--config") + 1] == "/var/lib/proxsync-agent/rclone.conf"

    def test_bandwidth_limit_is_expressed_in_kibibytes(self) -> None:
        options = RcloneOptions(rclone_bin=RCLONE, bwlimit_kbps=2048)
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:a")
        assert argv[argv.index("--bwlimit") + 1] == "2048k"

    def test_no_bandwidth_flag_when_unlimited(self, options: RcloneOptions) -> None:
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:a")
        assert "--bwlimit" not in argv

    def test_progress_flags_are_present_on_transfers(self, options: RcloneOptions) -> None:
        argv = build_copy_argv(options, source="/local/a", destination="gdrive:a")
        assert "--stats-one-line-date" in argv
        assert argv[argv.index("--stats") + 1] == "5s"

    def test_lsjson_requests_hashes(self, options: RcloneOptions) -> None:
        argv = build_lsjson_argv(options, target="gdrive:dump")
        assert argv[-2:] == ["--hash", "gdrive:dump"]

    def test_about_asks_for_json(self, options: RcloneOptions) -> None:
        argv = build_about_argv(options, remote_spec="gdrive:")
        assert argv[-2:] == ["--json", "gdrive:"]

    def test_delete_uses_deletefile_not_delete(self, options: RcloneOptions) -> None:
        """`delete` removes directory *contents*; `deletefile` refuses a directory."""
        argv = build_delete_argv(options, target="gdrive:dump/a.vma.zst")
        assert "deletefile" in argv
        assert "purge" not in argv
        assert "delete" not in argv


class TestProgressParsing:
    def test_parses_a_stats_line(self) -> None:
        parser = RcloneProgressParser()
        progress = TaskProgress()

        changed = parser.feed(
            "2026/07/26 01:15:03 NOTICE: Transferred:   \t  1.234 GiB / 5.000 GiB, "
            "24%, 45.123 MiB/s, ETA 1m23s",
            progress,
        )

        assert changed
        assert progress.percent == 24.0
        assert progress.bytes_total == int(5.0 * 1024**3)
        assert progress.bytes_done == int(1.234 * 1024**3)
        assert progress.rate_bps == int(45.123 * 1024**2)
        assert progress.eta_seconds == 83

    def test_parses_a_line_without_an_eta(self) -> None:
        parser = RcloneProgressParser()
        progress = TaskProgress()

        parser.feed("NOTICE: Transferred:  1 GiB / 2 GiB, 50%, 10 MiB/s, ETA -", progress)

        assert progress.percent == 50.0
        assert progress.eta_seconds is None

    def test_parses_the_bytes_only_form(self) -> None:
        parser = RcloneProgressParser()
        progress = TaskProgress()

        parser.feed("INFO  : Transferred:   1.234 GBytes (12.345 MBytes/s)", progress)

        assert progress.bytes_done == int(1.234 * 1000**3)
        assert progress.rate_bps == int(12.345 * 1000**2)

    def test_captures_an_error(self) -> None:
        parser = RcloneProgressParser()
        parser.feed(
            "2026/07/26 01:15:03 ERROR : a.vma.zst: Failed to copy: googleapi: quota exceeded",
            TaskProgress(),
        )
        assert parser.error_message is not None
        assert "quota exceeded" in parser.error_message

    def test_an_unrecognised_line_changes_nothing(self) -> None:
        """The parser never guesses."""
        parser = RcloneProgressParser()
        progress = TaskProgress()

        assert parser.feed("some unrelated chatter", progress) is False
        assert progress.percent is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1m23s", 83), ("2h3m4s", 7384), ("45s", 45), ("-", None), ("", None), ("soon", None)],
    )
    def test_eta_parsing(self, value: str, expected: int | None) -> None:
        assert parse_eta(value) == expected


class TestLsjsonParsing:
    def test_parses_entries_with_hashes(self) -> None:
        payload = json.dumps(
            [
                {
                    "Path": "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst",
                    "Name": "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst",
                    "Size": 5368709120,
                    "ModTime": "2026-07-26T01:15:03.000Z",
                    "IsDir": False,
                    "Hashes": {"md5": "d41d8cd98f00b204e9800998ecf8427e"},
                }
            ]
        )

        [entry] = parse_lsjson(payload)

        assert entry.name.startswith("vzdump-qemu-101")
        assert entry.size_bytes == 5368709120
        assert entry.is_dir is False
        assert entry.md5 == "d41d8cd98f00b204e9800998ecf8427e"
        assert entry.modified_at is not None
        assert entry.modified_at.tzinfo is not None

    def test_an_entry_without_hashes_reports_none(self) -> None:
        [entry] = parse_lsjson(json.dumps([{"Name": "a", "Size": 1, "IsDir": False}]))
        assert entry.md5 is None

    def test_empty_listing(self) -> None:
        assert parse_lsjson("[]") == []
        assert parse_lsjson("") == []

    def test_invalid_json_is_an_execution_failure(self) -> None:
        with pytest.raises(ExecutionFailed):
            parse_lsjson("not json at all")

    def test_a_non_list_payload_is_rejected(self) -> None:
        with pytest.raises(ExecutionFailed):
            parse_lsjson('{"error": "nope"}')


class TestAboutParsing:
    def test_parses_a_quota(self) -> None:
        quota = parse_about(
            json.dumps({"total": 16106127360, "used": 8053063680, "free": 8053063680})
        )

        assert quota.total_bytes == 16106127360
        assert quota.used_percent == 50.0

    def test_a_backend_without_a_quota_reports_none_not_zero(self) -> None:
        """Zero would render on the storage page as 'full'."""
        quota = parse_about(json.dumps({"used": 1234}))

        assert quota.total_bytes is None
        assert quota.free_bytes is None
        assert quota.used_percent is None
        assert quota.used_bytes == 1234

    def test_invalid_json_is_an_execution_failure(self) -> None:
        with pytest.raises(ExecutionFailed):
            parse_about("<html>error</html>")
