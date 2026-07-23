"""Sync endpoints through the full HTTP stack.

The question these answer is the same one the backup tests answer: which requests are
accepted, which are refused, and what argv would a request have produced.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.core.config import AgentSettings
from tests.conftest import (
    RCLONE_UPLOAD_OUTPUT,
    ArtifactFactory,
    FakeProcessRunner,
    SignedTestClient,
)

ARCHIVE = "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"
UPLOAD_REQUEST = {"filename": ARCHIVE, "remote": "gdrive", "remote_path": "proxsync/dump"}


def rclone_calls(runner: FakeProcessRunner) -> list[list[str]]:
    return [call for call in runner.calls if call[0].endswith("rclone")]


def lsjson_payload(*, name: str = ARCHIVE, size: int = 1024, md5: str | None = "x") -> str:
    entry: dict[str, object] = {
        "Path": name,
        "Name": name,
        "Size": size,
        "IsDir": False,
        "ModTime": "2026-07-26T01:15:03.000Z",
    }
    if md5 is not None:
        entry["Hashes"] = {"md5": md5}
    return json.dumps([entry])


class TestUpload:
    def test_accepts_a_valid_request(
        self,
        client: SignedTestClient,
        runner: FakeProcessRunner,
        artifact_factory: ArtifactFactory,
    ) -> None:
        artifact_factory(ARCHIVE, size=1024)
        runner.output = RCLONE_UPLOAD_OUTPUT

        response = client.post("/sync/upload", UPLOAD_REQUEST)

        assert response.status_code == 202
        body = response.json()
        assert body["kind"] == "upload"
        assert body["task_id"]

    def test_refuses_a_remote_outside_the_allow_list(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE)

        response = client.post("/sync/upload", {**UPLOAD_REQUEST, "remote": "local"})

        assert response.status_code == 400
        assert "allow-list" in response.json()["detail"]
        assert rclone_calls(runner) == [], "no process may be spawned for a refused request"

    def test_refuses_a_non_artifact_filename(
        self, client: SignedTestClient, runner: FakeProcessRunner, settings: AgentSettings
    ) -> None:
        (settings.dump_root / "secrets.env").write_text("TOKEN=1", encoding="utf-8")

        response = client.post("/sync/upload", {**UPLOAD_REQUEST, "filename": "secrets.env"})

        assert response.status_code == 400
        assert rclone_calls(runner) == []

    @pytest.mark.parametrize(
        "filename",
        [
            "../../etc/shadow",
            "/etc/shadow",
            "-rf",
            "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst\x00",
        ],
    )
    def test_refuses_traversal_and_flag_shaped_filenames(
        self, client: SignedTestClient, runner: FakeProcessRunner, filename: str
    ) -> None:
        response = client.post("/sync/upload", {**UPLOAD_REQUEST, "filename": filename})

        assert response.status_code in {400, 422}
        assert rclone_calls(runner) == []

    @pytest.mark.parametrize("remote_path", ["../../etc", "dump/*", "a:b"])
    def test_refuses_hostile_remote_paths(
        self, client: SignedTestClient, runner: FakeProcessRunner, remote_path: str
    ) -> None:
        response = client.post("/sync/upload", {**UPLOAD_REQUEST, "remote_path": remote_path})

        assert response.status_code == 400
        assert rclone_calls(runner) == []

    def test_missing_artifact_is_not_found(self, client: SignedTestClient) -> None:
        response = client.post("/sync/upload", UPLOAD_REQUEST)
        assert response.status_code == 404

    def test_reports_conflict_when_a_transfer_is_running(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE)
        client.container.slots.try_acquire("sync")

        response = client.post("/sync/upload", UPLOAD_REQUEST)

        assert response.status_code == 409


class TestDownload:
    def test_refuses_to_overwrite_a_local_artifact_by_default(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        """A download that replaced the local copy could destroy the only good backup."""
        artifact_factory(ARCHIVE, size=4096)

        response = client.post("/sync/download", UPLOAD_REQUEST)

        assert response.status_code == 400
        assert "overwrite=true" in response.json()["detail"]
        assert rclone_calls(runner) == []

    def test_overwrite_must_be_asked_for(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE, size=4096)
        runner.output = RCLONE_UPLOAD_OUTPUT

        response = client.post("/sync/download", {**UPLOAD_REQUEST, "overwrite": True})

        assert response.status_code == 202

    def test_a_hostile_remote_filename_cannot_escape_the_dump_root(
        self, client: SignedTestClient, runner: FakeProcessRunner
    ) -> None:
        """The name is validated on the way *in* as well as out."""
        response = client.post(
            "/sync/download", {**UPLOAD_REQUEST, "filename": "../../etc/cron.d/evil"}
        )

        assert response.status_code in {400, 422}
        assert rclone_calls(runner) == []


class TestVerify:
    def test_reports_a_match(
        self,
        client: SignedTestClient,
        runner: FakeProcessRunner,
        settings: AgentSettings,
        artifact_factory: ArtifactFactory,
    ) -> None:
        path = artifact_factory(ARCHIVE, size=2048)
        digest = hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - matching Drive's hash
        runner.capture_output = lsjson_payload(size=2048, md5=digest)

        body = client.post("/sync/verify", UPLOAD_REQUEST).json()

        assert body["outcome"] == "match"
        assert body["verified"] is True
        assert body["local_md5"] == digest

    def test_reports_a_corrupted_remote_copy(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        """The acceptance criterion: a deliberately corrupted remote copy is a mismatch."""
        artifact_factory(ARCHIVE, size=2048)
        runner.capture_output = lsjson_payload(size=2048, md5="0" * 32)

        body = client.post("/sync/verify", UPLOAD_REQUEST).json()

        assert body["outcome"] == "hash_mismatch"
        assert body["verified"] is False
        assert "corrupt" in body["detail"]

    def test_reports_a_size_mismatch(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE, size=2048)
        runner.capture_output = lsjson_payload(size=999)

        body = client.post("/sync/verify", UPLOAD_REQUEST).json()

        assert body["outcome"] == "size_mismatch"
        assert body["remote_size_bytes"] == 999

    def test_reports_a_missing_remote_copy(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(ARCHIVE)
        runner.capture_output = "[]"

        body = client.post("/sync/verify", UPLOAD_REQUEST).json()

        assert body["outcome"] == "missing_remote"
        assert body["verified"] is False

    def test_matching_size_without_a_hash_is_not_called_verified(
        self, client: SignedTestClient, runner: FakeProcessRunner, artifact_factory: ArtifactFactory
    ) -> None:
        """A truncated file of exactly the right length would otherwise pass."""
        artifact_factory(ARCHIVE, size=2048)
        runner.capture_output = lsjson_payload(size=2048, md5=None)

        body = client.post("/sync/verify", UPLOAD_REQUEST).json()

        assert body["outcome"] == "hash_unavailable"
        assert body["verified"] is False
        assert "unconfirmed" in body["detail"]


class TestListAndQuota:
    def test_lists_a_remote_directory(
        self, client: SignedTestClient, runner: FakeProcessRunner
    ) -> None:
        runner.capture_output = lsjson_payload(size=4096, md5="abc")

        body = client.get("/sync/list?remote=gdrive&remote_path=proxsync/dump").json()

        assert body["remote"] == "gdrive"
        assert body["entries"][0]["size_bytes"] == 4096
        assert body["entries"][0]["md5"] == "abc"

    def test_a_missing_remote_directory_lists_empty(
        self, client: SignedTestClient, runner: FakeProcessRunner
    ) -> None:
        """A folder that does not exist yet is not an error — it is an empty backup set."""
        runner.capture_exit_code = 3
        runner.capture_output = "ERROR: directory not found"

        body = client.get("/sync/list?remote=gdrive&remote_path=proxsync/dump").json()

        assert body["entries"] == []

    def test_refuses_a_remote_outside_the_allow_list(self, client: SignedTestClient) -> None:
        response = client.get("/sync/list?remote=local")
        assert response.status_code == 400

    def test_reports_quota(self, client: SignedTestClient, runner: FakeProcessRunner) -> None:
        runner.capture_output = json.dumps(
            {"total": 16106127360, "used": 4026531840, "free": 12079595520}
        )

        body = client.get("/sync/about?remote=gdrive").json()

        assert body["total_bytes"] == 16106127360
        assert body["used_percent"] == 25.0


class TestDeleteRemote:
    def test_deletes_one_file(self, client: SignedTestClient, runner: FakeProcessRunner) -> None:
        runner.capture_exit_code = 0
        runner.capture_output = ""

        body = client.post("/sync/delete", UPLOAD_REQUEST).json()

        assert body["deleted"] == f"gdrive:proxsync/dump/{ARCHIVE}"
        [argv] = rclone_calls(runner)
        assert "deletefile" in argv

    def test_refuses_a_non_artifact_name(
        self, client: SignedTestClient, runner: FakeProcessRunner
    ) -> None:
        response = client.post("/sync/delete", {**UPLOAD_REQUEST, "filename": "important.sql"})

        assert response.status_code == 400
        assert rclone_calls(runner) == []


class TestSyncDisabled:
    def test_every_sync_route_refuses_when_sync_is_off(
        self, settings: AgentSettings, runner: FakeProcessRunner, tmp_path: Path
    ) -> None:
        from fastapi.testclient import TestClient

        from app.main import create_app
        from tests.conftest import SignedTestClient as Signed

        del tmp_path
        settings.sync_enabled = False
        app = create_app(settings)
        app.state.container.sync_service._runner = runner  # noqa: SLF001

        with TestClient(app, raise_server_exceptions=False) as raw:
            disabled = Signed(raw)
            assert disabled.post("/sync/upload", UPLOAD_REQUEST).status_code == 400
            assert disabled.get("/sync/list?remote=gdrive").status_code == 400

        assert rclone_calls(runner) == []
