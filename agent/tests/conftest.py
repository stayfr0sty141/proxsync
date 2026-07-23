"""Shared fixtures.

No test touches a real Proxmox host: the process runner is replaced with a fake that records
argv and replays canned command output. What is exercised is exactly what matters — which
requests are accepted, which are refused, and what argv a request would have produced.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import Container, build_container
from app.core.config import AgentSettings
from app.core.security import sign_request
from app.executors.base import (
    LineHandler,
    ProcessHandle,
    ProcessResult,
    ProcessRunner,
    validate_argv,
)
from app.main import create_app

HMAC_SECRET = "test-secret-value"  # noqa: S105 - test fixture
KEY_ID = "proxsync-test"

QEMU_BACKUP_OUTPUT = """\
INFO: starting new backup job: vzdump 101 --mode snapshot --compress zstd --storage backup-hdd
INFO: Starting Backup of VM 101 (qemu)
INFO: creating vzdump archive '{archive}'
INFO: status: 12% (3865470976/30601641984), sparse 0% (0), duration 33, read/write 117/117 MB/s
INFO: status: 100% (30601641984/30601641984), sparse 4% (1288490188), duration 312, read/write 98/94 MB/s
INFO: archive file size: 8.50GB
INFO: Finished Backup of VM 101 (00:05:12)
INFO: Backup job finished successfully
"""

LXC_BACKUP_OUTPUT = """\
INFO: Starting Backup of VM 104 (lxc)
INFO: creating vzdump archive '{archive}'
INFO: Total bytes written: 2274918400 (2.2GiB, 45MiB/s)
INFO: Finished Backup of VM 104 (00:01:48)
"""

RESTORE_OUTPUT = """\
progress 25% (read 2147483648 bytes, duration 12 sec)
progress 100% (read 8589934592 bytes, duration 48 sec)
"""

# rclone --stats-one-line-date, as emitted during a copyto.
RCLONE_UPLOAD_OUTPUT = """\
2026/07/26 01:15:03 NOTICE: Transferred:   	  1.234 GiB / 5.000 GiB, 24%, 45.123 MiB/s, ETA 1m23s
2026/07/26 01:15:08 NOTICE: Transferred:   	  5.000 GiB / 5.000 GiB, 100%, 48.000 MiB/s, ETA 0s
2026/07/26 01:15:08 NOTICE: Transferred:   	        1 / 1, 100%
"""


class FakeProcessRunner(ProcessRunner):
    """Records argv and replays scripted output instead of spawning anything."""

    def __init__(self) -> None:
        super().__init__(cancel_grace_seconds=1)
        self.calls: list[list[str]] = []
        self.logged_calls: list[list[str]] = []
        self.output: str = ""
        self.exit_code: int = 0
        self.capture_output: str = "status: stopped"
        self.capture_exit_code: int = 0
        self.creates_archive: Path | None = None

    async def run_logged(
        self,
        argv: Sequence[str],
        *,
        log_path: Path,
        timeout_seconds: int,
        on_line: LineHandler | None = None,
        handle: ProcessHandle | None = None,
    ) -> ProcessResult:
        command = validate_argv(argv)
        self.calls.append(command)
        self.logged_calls.append(command)

        if self.creates_archive is not None:
            self.creates_archive.parent.mkdir(parents=True, exist_ok=True)
            self.creates_archive.write_bytes(b"fake-archive-payload")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = self.output.splitlines()
        with log_path.open("a", encoding="utf-8") as handle_file:
            for line in lines:
                handle_file.write(line + "\n")
                if on_line is not None:
                    on_line(line)

        return ProcessResult(
            exit_code=self.exit_code,
            duration_seconds=0.01,
            timed_out=False,
            cancelled=False,
            last_lines=tuple(lines[-5:]),
        )

    async def run_capture(self, argv: Sequence[str], *, timeout_seconds: int) -> tuple[int, str]:
        self.calls.append(validate_argv(argv))
        return self.capture_exit_code, self.capture_output


@pytest.fixture
def settings(tmp_path: Path) -> AgentSettings:
    dump_root = tmp_path / "dump"
    qemu_dir = tmp_path / "etc" / "qemu-server"
    lxc_dir = tmp_path / "etc" / "lxc"
    for directory in (dump_root, qemu_dir, lxc_dir, tmp_path / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)

    # Guests that "exist" on this fake host.
    (qemu_dir / "101.conf").write_text("name: docker-host\n", encoding="utf-8")
    (lxc_dir / "104.conf").write_text("hostname: homeassistant\n", encoding="utf-8")

    return AgentSettings(
        bind_host="127.0.0.1",
        bind_port=8765,
        log_json=False,
        log_level="WARNING",
        allowed_client_networks=[],  # TestClient reports a non-IP host
        api_key_id=KEY_ID,
        hmac_secret=SecretStr(HMAC_SECRET),
        dump_root=dump_root,
        temp_dir=tmp_path / "tmp",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "log",
        qemu_config_dir=qemu_dir,
        lxc_config_dir=lxc_dir,
        allowed_backup_storages=["backup-hdd"],
        allowed_restore_storages=["local-lvm"],
        rclone_bin=tmp_path / "bin" / "rclone",
        rclone_config=tmp_path / "rclone.conf",
        allowed_remotes=["gdrive"],
        sync_enabled=True,
        max_concurrent_syncs=1,
        verify_storage_with_pvesm=False,
        checksum_after_backup=True,
        max_concurrent_backups=1,
        max_concurrent_restores=1,
    )


@pytest.fixture
def runner() -> FakeProcessRunner:
    return FakeProcessRunner()


@pytest.fixture
def container(settings: AgentSettings, runner: FakeProcessRunner) -> Container:
    settings.ensure_directories()
    built = build_container(settings)
    _replace_runner(built, runner)
    built.registry.load()
    return built


def _replace_runner(container: Container, runner: FakeProcessRunner) -> None:
    """Swap the process runner into every collaborator that holds one."""
    container.runner = runner
    container.backup_service._runner = runner  # noqa: SLF001 - deliberate test seam
    container.restore_service._runner = runner  # noqa: SLF001
    container.storage_service._pvesm._runner = runner  # noqa: SLF001
    container.sync_service._runner = runner  # noqa: SLF001


@pytest.fixture
def client(settings: AgentSettings, runner: FakeProcessRunner) -> Iterator[SignedTestClient]:
    app = create_app(settings)
    _replace_runner(app.state.container, runner)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield SignedTestClient(test_client)


class SignedTestClient:
    """TestClient wrapper that signs every request the way the dashboard will."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    @property
    def raw(self) -> TestClient:
        return self._client

    @property
    def container(self) -> Container:
        state: Container = self._client.app.state.container  # type: ignore[attr-defined]
        return state

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        return {
            "X-ProxSync-Key": KEY_ID,
            "X-ProxSync-Timestamp": timestamp,
            "X-ProxSync-Nonce": nonce,
            "X-ProxSync-Signature": sign_request(
                secret=HMAC_SECRET,
                method=method,
                path=path,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
            ),
        }

    def request(self, method: str, path: str, *, json: object | None = None):  # type: ignore[no-untyped-def]
        import json as json_module

        body = b"" if json is None else json_module.dumps(json).encode()
        headers = self._headers(method, path, body)
        if json is not None:
            headers["Content-Type"] = "application/json"
        return self._client.request(method, path, content=body, headers=headers)

    def get(self, path: str):  # type: ignore[no-untyped-def]
        return self.request("GET", path)

    def post(self, path: str, json: object | None = None):  # type: ignore[no-untyped-def]
        return self.request("POST", path, json=json)

    def delete(self, path: str):  # type: ignore[no-untyped-def]
        return self.request("DELETE", path)


class ArtifactFactory(Protocol):
    def __call__(self, filename: str, *, size: int = ..., notes: str | None = ...) -> Path: ...


@pytest.fixture
def artifact_factory(settings: AgentSettings) -> ArtifactFactory:
    """Create a fake backup artifact in the dump root."""

    def _create(filename: str, *, size: int = 1024, notes: str | None = None) -> Path:
        path = settings.dump_root / filename
        path.write_bytes(b"x" * size)
        if notes is not None:
            path.with_name(path.name + ".notes").write_text(notes, encoding="utf-8")
        return path

    return _create
