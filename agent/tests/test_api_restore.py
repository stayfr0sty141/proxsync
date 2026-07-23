"""Restore endpoints — the guards that must hold on the host itself."""

from __future__ import annotations

import hashlib

from app.core.config import AgentSettings
from tests.conftest import ArtifactFactory, FakeProcessRunner, SignedTestClient

VM_ARCHIVE = "vzdump-qemu-101-2026_07_19-01_00_04.vma.zst"
LXC_ARCHIVE = "vzdump-lxc-104-2026_07_19-01_07_22.tar.zst"


def vm_request(**overrides: object) -> dict[str, object]:
    return {
        "archive": VM_ARCHIVE,
        "target_vmid": 151,
        "storage": "local-lvm",
        **overrides,
    }


class TestRestoreVm:
    def test_accepts_a_valid_restore_to_a_free_vmid(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory, runner: FakeProcessRunner
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        response = client.post("/restore/vm", vm_request())

        assert response.status_code == 202
        assert response.json()["kind"] == "restore_vm"

    def test_rejects_missing_archive(self, client: SignedTestClient) -> None:
        response = client.post("/restore/vm", vm_request())
        assert response.status_code == 404

    def test_rejects_an_lxc_archive(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(LXC_ARCHIVE)
        response = client.post("/restore/vm", vm_request(archive=LXC_ARCHIVE))

        assert response.status_code == 400
        assert "lxc restore endpoint" in response.json()["detail"]

    def test_rejects_path_traversal_in_archive(self, client: SignedTestClient) -> None:
        response = client.post("/restore/vm", vm_request(archive="../../etc/passwd"))
        assert response.status_code == 400

    def test_rejects_non_artifact_archive(self, client: SignedTestClient) -> None:
        response = client.post("/restore/vm", vm_request(archive="payload.sh"))
        assert response.status_code == 400
        assert "not a vzdump artifact" in response.json()["detail"]

    def test_rejects_storage_outside_the_allow_list(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        response = client.post("/restore/vm", vm_request(storage="backup-hdd"))

        assert response.status_code == 400
        assert "allow-list" in response.json()["detail"]

    def test_refuses_existing_vmid_without_overwrite(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        response = client.post("/restore/vm", vm_request(target_vmid=101))

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]

    def test_refuses_running_guest_without_force_stop(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory, runner: FakeProcessRunner
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        runner.capture_output = "status: running\n"

        response = client.post("/restore/vm", vm_request(target_vmid=101, overwrite=True))

        assert response.status_code == 423
        assert "force_stop" in response.json()["detail"]

    def test_allows_running_guest_with_force_stop(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory, runner: FakeProcessRunner
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        runner.capture_output = "status: running\n"

        response = client.post(
            "/restore/vm", vm_request(target_vmid=101, overwrite=True, force_stop=True)
        )

        assert response.status_code == 202

    def test_refuses_replacing_a_container_with_a_vm(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        response = client.post("/restore/vm", vm_request(target_vmid=104, overwrite=True))

        assert response.status_code == 400
        assert "cannot be replaced" in response.json()["detail"]

    def test_verifies_expected_digest_before_restoring(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory, runner: FakeProcessRunner
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        wrong_digest = "0" * 64

        response = client.post("/restore/vm", vm_request(expected_sha256=wrong_digest))

        assert response.status_code == 400
        assert "digest does not match" in response.json()["detail"]
        assert runner.logged_calls == []

    def test_accepts_matching_digest(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory, settings: AgentSettings
    ) -> None:
        path = artifact_factory(VM_ARCHIVE, size=512)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()

        response = client.post("/restore/vm", vm_request(expected_sha256=digest))

        assert response.status_code == 202

    def test_reports_conflict_when_a_restore_is_running(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        client.container.slots.try_acquire("restore")

        response = client.post("/restore/vm", vm_request())

        assert response.status_code == 409
        assert "serialised" in response.json()["detail"]


class TestRestoreLxc:
    def test_accepts_a_valid_container_restore(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(LXC_ARCHIVE)
        response = client.post(
            "/restore/lxc",
            {"archive": LXC_ARCHIVE, "target_vmid": 154, "storage": "local-lvm"},
        )

        assert response.status_code == 202
        assert response.json()["kind"] == "restore_lxc"

    def test_rejects_a_vm_archive(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        response = client.post(
            "/restore/lxc",
            {"archive": VM_ARCHIVE, "target_vmid": 154, "storage": "local-lvm"},
        )

        assert response.status_code == 400
        assert "vm restore endpoint" in response.json()["detail"]

    def test_accepts_unprivileged_flag(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(LXC_ARCHIVE)
        response = client.post(
            "/restore/lxc",
            {
                "archive": LXC_ARCHIVE,
                "target_vmid": 154,
                "storage": "local-lvm",
                "unprivileged": True,
            },
        )

        assert response.status_code == 202


class TestTaskEndpoints:
    def test_unknown_task_is_not_found(self, client: SignedTestClient) -> None:
        response = client.get("/task/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_malformed_task_id_is_rejected(self, client: SignedTestClient) -> None:
        response = client.get("/task/../../etc/passwd")
        assert response.status_code in {404, 422}

    def test_task_appears_after_a_restore_is_accepted(
        self, client: SignedTestClient, artifact_factory: ArtifactFactory
    ) -> None:
        artifact_factory(VM_ARCHIVE)
        task_id = client.post("/restore/vm", vm_request()).json()["task_id"]

        response = client.get(f"/task/{task_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["task_id"] == task_id
        assert body["meta"]["target_vmid"] == 151
        assert body["meta"]["archive"] == VM_ARCHIVE

    def test_cancelling_a_finished_task_is_rejected(self, client: SignedTestClient) -> None:
        from app.schemas.enums import TaskKind, TaskState

        registry = client.container.registry
        task = registry.create(kind=TaskKind.BACKUP)
        registry.finish(task, state=TaskState.SUCCESS, exit_code=0)

        response = client.post(f"/task/{task.id}/cancel")

        assert response.status_code == 400
        assert "already finished" in response.json()["detail"]

    def test_task_log_is_returned_when_present(self, client: SignedTestClient) -> None:
        from app.schemas.enums import TaskKind

        registry = client.container.registry
        task = registry.create(kind=TaskKind.BACKUP)
        task.log_path.parent.mkdir(parents=True, exist_ok=True)
        task.log_path.write_text("line one\nline two\nline three\n", encoding="utf-8")

        response = client.get(f"/task/{task.id}/log?tail=2")

        assert response.status_code == 200
        body = response.json()
        assert body["lines"] == ["line two", "line three"]
        assert body["truncated"] is True
        assert body["total_lines"] == 3

    def test_missing_log_is_not_found(self, client: SignedTestClient) -> None:
        from app.schemas.enums import TaskKind

        task = client.container.registry.create(kind=TaskKind.BACKUP)
        response = client.get(f"/task/{task.id}/log")
        assert response.status_code == 404
