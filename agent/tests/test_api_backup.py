"""Backup endpoints through the full HTTP stack."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from app.core.config import AgentSettings
from tests.conftest import QEMU_BACKUP_OUTPUT, ArtifactFactory, FakeProcessRunner, SignedTestClient

VALID_REQUEST = {
    "vmid": 101,
    "guest_type": "vm",
    "mode": "snapshot",
    "compression": "zstd",
    "storage": "backup-hdd",
}


class TestStartBackup:
    def test_accepts_a_valid_request(
        self, client: SignedTestClient, runner: FakeProcessRunner
    ) -> None:
        runner.output = QEMU_BACKUP_OUTPUT.format(archive="/tmp/x.vma.zst")
        response = client.post("/backup/start", VALID_REQUEST)

        assert response.status_code == 202
        body = response.json()
        assert body["state"] in {"queued", "running"}
        assert body["kind"] == "backup"
        assert body["task_id"]

    def test_rejects_unknown_vmid(self, client: SignedTestClient) -> None:
        response = client.post("/backup/start", {**VALID_REQUEST, "vmid": 999})
        assert response.status_code == 404
        assert "999" in response.json()["detail"]

    def test_rejects_guest_type_mismatch(self, client: SignedTestClient) -> None:
        # 101 exists, but it is a VM, not a container.
        response = client.post("/backup/start", {**VALID_REQUEST, "guest_type": "lxc"})
        assert response.status_code == 400
        assert "not a lxc" in response.json()["detail"]

    def test_rejects_storage_outside_the_allow_list(self, client: SignedTestClient) -> None:
        response = client.post("/backup/start", {**VALID_REQUEST, "storage": "local-lvm"})
        assert response.status_code == 400
        assert "allow-list" in response.json()["detail"]

    def test_rejects_unknown_backup_mode(self, client: SignedTestClient) -> None:
        response = client.post("/backup/start", {**VALID_REQUEST, "mode": "rm -rf /"})
        assert response.status_code == 422

    def test_rejects_unknown_fields(self, client: SignedTestClient) -> None:
        response = client.post("/backup/start", {**VALID_REQUEST, "extra_flag": "--exec"})
        assert response.status_code == 422

    def test_rejects_out_of_range_vmid(self, client: SignedTestClient) -> None:
        response = client.post("/backup/start", {**VALID_REQUEST, "vmid": 1})
        assert response.status_code == 422

    def test_rejects_hostile_notes(self, client: SignedTestClient) -> None:
        response = client.post("/backup/start", {**VALID_REQUEST, "notes": "$(rm -rf /)"})
        assert response.status_code == 422

    def test_reports_conflict_when_the_slot_is_held(self, client: SignedTestClient) -> None:
        client.container.slots.try_acquire("backup")
        response = client.post("/backup/start", VALID_REQUEST)

        assert response.status_code == 409
        assert "already running" in response.json()["detail"]


class TestListBackups:
    def test_lists_only_vzdump_artifacts(
        self, client: SignedTestClient, settings: AgentSettings, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst", size=2048)
        artifact_factory("vzdump-lxc-104-2026_07_19-01_07_22.tar.zst", size=1024)
        (settings.dump_root / "notes.txt").write_text("not a backup", encoding="utf-8")
        (settings.dump_root / "database.sql").write_text("also not", encoding="utf-8")

        response = client.get("/backup/list")

        assert response.status_code == 200
        names = {entry["filename"] for entry in response.json()}
        assert names == {
            "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst",
            "vzdump-lxc-104-2026_07_19-01_07_22.tar.zst",
        }

    def test_filters_by_vmid(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst")
        artifact_factory("vzdump-lxc-104-2026_07_19-01_07_22.tar.zst")

        response = client.get("/backup/list?vmid=104")

        assert [entry["vmid"] for entry in response.json()] == [104]

    def test_filters_by_guest_type(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst")
        artifact_factory("vzdump-lxc-104-2026_07_19-01_07_22.tar.zst")

        response = client.get("/backup/list?guest_type=vm")

        assert [entry["guest_type"] for entry in response.json()] == ["vm"]

    def test_reports_metadata(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(
            "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst", size=4096, notes="proxsync run 812"
        )

        entry = client.get("/backup/list").json()[0]

        assert entry["size_bytes"] == 4096
        assert entry["compression"] == "zstd"
        assert entry["notes"] == "proxsync run 812"
        assert entry["checksum_sha256"] is None  # not computed until a backup task runs

    def test_sorted_newest_first(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory("vzdump-qemu-101-2025_01_01-01_00_00.vma.zst")
        artifact_factory("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst")

        filenames = [entry["filename"] for entry in client.get("/backup/list").json()]

        assert filenames[0] == "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"


class TestDeleteBackup:
    FILENAME = "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"

    def test_deletes_artifact_and_sidecars(
        self, client: SignedTestClient, settings: AgentSettings, artifact_factory: ArtifactFactory
    ) -> None:
        artifact = artifact_factory(self.FILENAME, size=1024, notes="run 812")
        log = settings.dump_root / "vzdump-qemu-101-2026_07_19-01_00_04.log"
        log.write_text("backup log", encoding="utf-8")
        checksum = settings.dump_root / f"{self.FILENAME}.sha256"
        checksum.write_text("abc  file\n", encoding="utf-8")
        unrelated = settings.dump_root / "keep-me.txt"
        unrelated.write_text("untouched", encoding="utf-8")

        response = client.delete(f"/backup/{self.FILENAME}")

        assert response.status_code == 200
        assert response.json()["freed_bytes"] > 1024
        assert not artifact.exists()
        assert not log.exists()
        assert not checksum.exists()
        assert unrelated.exists()

    def test_refuses_non_artifact_filenames(
        self, client: SignedTestClient, settings: AgentSettings
    ) -> None:
        victim = settings.dump_root / "important.sql"
        victim.write_text("payload", encoding="utf-8")

        response = client.delete("/backup/important.sql")

        assert response.status_code == 400
        assert victim.exists()

    def test_refuses_path_traversal(
        self, client: SignedTestClient, settings: AgentSettings
    ) -> None:
        outside = settings.dump_root.parent / "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"
        outside.write_text("outside the root", encoding="utf-8")

        response = client.delete("/backup/..%2Fvzdump-qemu-101-2026_07_19-01_00_04.vma.zst")

        assert response.status_code in {400, 404}
        assert outside.exists()

    def test_missing_artifact_is_not_found(self, client: SignedTestClient) -> None:
        response = client.delete(f"/backup/{self.FILENAME}")
        assert response.status_code == 404


class TestStorageEndpoint:
    def test_reports_dump_root_usage(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory, runner: FakeProcessRunner
    ) -> None:
        artifact_factory("vzdump-qemu-101-2026_07_19-01_00_04.vma.zst", size=8192)
        runner.capture_output = (
            "Name             Type     Status           Total            Used       Available        %\n"
            "backup-hdd        dir     active       488384352       312516608       151011744   64.00%\n"
        )

        response = client.get("/storage/status")

        assert response.status_code == 200
        body = response.json()
        assert body["artifact_count"] == 1
        assert body["artifact_bytes"] == 8192
        assert body["dump_root"]["total_bytes"] > 0
        assert body["storages"][0]["name"] == "backup-hdd"


class TestHealthEndpoint:
    def test_health_needs_no_signature(self, client: SignedTestClient) -> None:
        response = client.raw.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["version"]
        assert {slot["name"] for slot in body["slots"]} == {"backup", "restore", "sync"}

    def test_reports_degraded_without_proxmox_binaries(
        self, client: SignedTestClient, settings: AgentSettings
    ) -> None:
        # The test host has no vzdump, so health must say so rather than claim "ok".
        body = client.raw.get("/health").json()
        assert body["status"] == "degraded"
        assert body["binaries"]["vzdump"] is False
        assert body["dump_root"] == str(settings.dump_root)


def test_unsigned_requests_are_rejected(client: SignedTestClient) -> None:
    response = client.raw.post("/backup/start", json=VALID_REQUEST)
    assert response.status_code == 401
    assert response.json()["title"] == "Authentication failed"


def test_openapi_document_lists_only_the_intended_surface(client: SignedTestClient) -> None:
    paths = set(client.raw.get("/openapi.json").json()["paths"])
    assert paths == {
        "/health",
        "/backup/start",
        "/backup/list",
        "/backup/{artifact_id}",
        "/restore/vm",
        "/restore/lxc",
        "/task",
        "/task/{task_id}",
        "/task/{task_id}/log",
        "/task/{task_id}/cancel",
        "/storage/status",
        # M4 extension endpoints (decision D1): rclone runs where the artifacts are.
        "/sync/upload",
        "/sync/download",
        "/sync/verify",
        "/sync/list",
        "/sync/about",
        "/sync/delete",
    }


def test_dump_root_is_untouched_by_a_rejected_request(
    client: SignedTestClient, settings: AgentSettings
) -> None:
    before = sorted(path.name for path in settings.dump_root.iterdir())
    client.post("/backup/start", {**VALID_REQUEST, "vmid": 999})
    after = sorted(path.name for path in settings.dump_root.iterdir())
    assert before == after


def test_no_process_is_spawned_for_rejected_requests(
    client: SignedTestClient, runner: FakeProcessRunner
) -> None:
    client.post("/backup/start", {**VALID_REQUEST, "vmid": 999})
    client.post("/backup/start", {**VALID_REQUEST, "storage": "evil"})
    client.post("/backup/start", {**VALID_REQUEST, "mode": "wipe"})
    assert runner.logged_calls == []


def test_paths_are_absolute_in_every_command(tmp_path: Path) -> None:
    """Guards the invariant that ProcessRunner enforces at execution time."""
    from app.core.config import AgentSettings as Settings

    defaults = Settings(hmac_secret=SecretStr("x"))
    for binary in (
        defaults.vzdump_bin,
        defaults.qmrestore_bin,
        defaults.qm_bin,
        defaults.pct_bin,
        defaults.pvesm_bin,
    ):
        assert binary.is_absolute()
