"""Manual backup, history and run endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.backup import BackupHistory
from app.schemas.enums import BackupStatus, GuestType, UploadStatus

from .conftest import ApiClient, StubAgentTransport, make_guest, seed_guests

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"


async def seed_backup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    vmid: int = 101,
    status: BackupStatus = BackupStatus.SUCCESS,
    filename: str | None = ARCHIVE,
    guest_name: str = "web",
    task_id: str | None = "task-1",
    locked: bool = False,
    started_at: datetime | None = None,
) -> int:
    async with session_factory() as session:
        record = BackupHistory(
            run_id=None,
            guest_id=None,
            vmid=vmid,
            guest_type=GuestType.VM.value,
            guest_name=guest_name,
            node="pve",
            storage="backup-hdd",
            filename=filename,
            local_path=f"/mnt/backup-hdd/dump/{filename}" if filename else None,
            size_bytes=5_368_709_120,
            status=status.value,
            upload_status=UploadStatus.NOT_REQUIRED.value,
            agent_task_id=task_id,
            retention_locked=locked,
            started_at=started_at or datetime.now(UTC),
        )
        session.add(record)
        await session.commit()
        return record.id


class TestManualBackup:
    async def test_queues_a_run_and_returns_202(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))

        response = authenticated_client.post(
            "/api/v1/backups/run",
            {"targets": [{"vmid": 101, "guest_type": "vm"}], "mode": "snapshot"},
        )

        assert response.status_code == 202
        run = response.json()["run"]
        assert run["status"] == "queued"
        assert run["guest_total"] == 1
        assert run["targets"][0]["guest_name"] == "web"

    async def test_a_guest_outside_the_allow_list_is_refused(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(103, name="scratch", enabled=False))

        response = authenticated_client.post(
            "/api/v1/backups/run", {"targets": [{"vmid": 103, "guest_type": "vm"}]}
        )

        assert response.status_code == 400
        assert "not enabled for backup" in str(response.json()["problems"])

    async def test_an_unknown_guest_is_refused(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.post(
            "/api/v1/backups/run", {"targets": [{"vmid": 999, "guest_type": "vm"}]}
        )

        assert response.status_code == 400
        assert "not in the inventory" in str(response.json()["problems"])

    async def test_an_empty_target_list_is_rejected(self, authenticated_client: ApiClient) -> None:
        response = authenticated_client.post("/api/v1/backups/run", {"targets": []})
        assert response.status_code == 422

    async def test_a_viewer_cannot_start_a_backup(
        self,
        client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from app.db.models.user import User
        from app.schemas.enums import UserRole

        client.login()
        client.post(
            "/api/v1/auth/change-password",
            {"current_password": "bootstrap-password-1", "new_password": "viewer-password-4"},
        )
        client.login(password="viewer-password-4")

        async with session_factory() as session:
            admin = await session.get(User, 1)
            assert admin is not None
            admin.role = UserRole.VIEWER.value
            await session.commit()

        response = client.post(
            "/api/v1/backups/run", {"targets": [{"vmid": 101, "guest_type": "vm"}]}
        )
        assert response.status_code == 403


class TestHistory:
    async def test_lists_backups_newest_first(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        older = datetime.now(UTC) - timedelta(days=7)
        await seed_backup(session_factory, vmid=101, started_at=older, filename="older.vma.zst")
        await seed_backup(session_factory, vmid=102, guest_name="db", filename="newer.vma.zst")

        body = authenticated_client.get("/api/v1/backups").json()

        assert body["total"] == 2
        assert [item["vmid"] for item in body["items"]] == [102, 101]

    async def test_filters_by_guest_and_status(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_backup(session_factory, vmid=101, filename="a.vma.zst")
        await seed_backup(
            session_factory, vmid=102, status=BackupStatus.FAILED, filename="b.vma.zst"
        )

        by_vmid = authenticated_client.get("/api/v1/backups?vmid=101").json()
        by_status = authenticated_client.get("/api/v1/backups?status=failed").json()

        assert [item["vmid"] for item in by_vmid["items"]] == [101]
        assert [item["vmid"] for item in by_status["items"]] == [102]

    async def test_deleted_artifacts_are_hidden_by_default_but_still_there(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A backup that vanished without trace is indistinguishable from one never taken."""
        await seed_backup(session_factory, status=BackupStatus.DELETED, filename="gone.vma.zst")

        default = authenticated_client.get("/api/v1/backups").json()
        including = authenticated_client.get("/api/v1/backups?include_deleted=true").json()

        assert default["total"] == 0
        assert including["total"] == 1

    async def test_paginates(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        for index in range(5):
            await seed_backup(session_factory, vmid=100 + index, filename=f"a{index}.vma.zst")

        body = authenticated_client.get("/api/v1/backups?limit=2&offset=2").json()

        assert len(body["items"]) == 2
        assert body["total"] == 5
        assert (body["limit"], body["offset"]) == (2, 2)

    async def test_unknown_backup_is_a_404(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.get("/api/v1/backups/9999").status_code == 404


class TestLog:
    async def test_reads_the_agents_task_log(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        agent_transport.queue(
            200,
            {
                "task_id": "task-1",
                "lines": ["INFO: starting new backup job", "INFO: Finished Backup of VM 101"],
                "truncated": False,
                "total_lines": 2,
            },
        )

        body = authenticated_client.get(f"/api/v1/backups/{backup_id}/log").json()

        assert body["total_lines"] == 2
        assert "Finished Backup" in body["lines"][1]

    async def test_a_backup_that_never_started_has_no_log(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(
            session_factory, status=BackupStatus.FAILED, filename=None, task_id=None
        )

        response = authenticated_client.get(f"/api/v1/backups/{backup_id}/log")

        assert response.status_code == 404
        assert "no agent task" in response.json()["detail"]


class TestDelete:
    async def test_deletes_the_artifact_and_keeps_the_row(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        agent_transport.queue(
            200,
            {
                "filename": ARCHIVE,
                "deleted": [ARCHIVE, f"{ARCHIVE}.notes"],
                "freed_bytes": 5_368_709_120,
            },
        )

        response = authenticated_client.delete(f"/api/v1/backups/{backup_id}")

        assert response.status_code == 200
        assert response.json()["freed_bytes"] == 5_368_709_120

        detail = authenticated_client.get(f"/api/v1/backups/{backup_id}").json()
        assert detail["status"] == "deleted"
        assert detail["local_deleted_at"] is not None

    async def test_refuses_a_pinned_backup(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory, locked=True)

        response = authenticated_client.delete(f"/api/v1/backups/{backup_id}")

        assert response.status_code == 409
        assert "pinned" in response.json()["detail"]
        assert agent_transport.requests == [], "the agent must not be asked at all"

    async def test_refuses_a_running_backup(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory, status=BackupStatus.RUNNING)

        response = authenticated_client.delete(f"/api/v1/backups/{backup_id}")

        assert response.status_code == 409
        assert "still running" in response.json()["detail"]

    async def test_a_row_with_no_artifact_has_nothing_to_delete(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory, status=BackupStatus.FAILED, filename=None)

        response = authenticated_client.delete(f"/api/v1/backups/{backup_id}")

        assert response.status_code == 400
        assert "nothing to delete" in response.json()["detail"]


class TestRetentionLock:
    async def test_pin_and_unpin(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)
        worker = authenticated_client.raw.app.state.container.retention_worker  # type: ignore[attr-defined]
        queued = Mock()
        worker.notify = queued

        pinned = authenticated_client.patch(
            f"/api/v1/backups/{backup_id}/lock", {"retention_locked": True}
        )
        assert pinned.json()["retention_locked"] is True

        unpinned = authenticated_client.patch(
            f"/api/v1/backups/{backup_id}/lock", {"retention_locked": False}
        )
        assert unpinned.json()["retention_locked"] is False
        assert queued.call_args_list == [call(backup_id), call(backup_id)]


class TestRuns:
    async def test_lists_runs_and_their_artifacts(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))
        run = authenticated_client.post(
            "/api/v1/backups/run", {"targets": [{"vmid": 101, "guest_type": "vm"}]}
        ).json()["run"]

        listing = authenticated_client.get("/api/v1/runs").json()
        detail = authenticated_client.get(f"/api/v1/runs/{run['id']}").json()
        artifacts = authenticated_client.get(f"/api/v1/runs/{run['id']}/backups").json()

        assert listing["total"] == 1
        assert detail["status"] == "queued"
        assert artifacts["total"] == 0, "nothing has run yet; workers are off in tests"

    async def test_cancelling_a_queued_run(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))
        run = authenticated_client.post(
            "/api/v1/backups/run", {"targets": [{"vmid": 101, "guest_type": "vm"}]}
        ).json()["run"]

        response = authenticated_client.post(f"/api/v1/runs/{run['id']}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

    async def test_cancelling_twice_is_a_conflict(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await seed_guests(session_factory, make_guest(101, name="web"))
        run = authenticated_client.post(
            "/api/v1/backups/run", {"targets": [{"vmid": 101, "guest_type": "vm"}]}
        ).json()["run"]

        authenticated_client.post(f"/api/v1/runs/{run['id']}/cancel")
        response = authenticated_client.post(f"/api/v1/runs/{run['id']}/cancel")

        assert response.status_code == 409

    async def test_unknown_run_is_a_404(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.get("/api/v1/runs/9999").status_code == 404
