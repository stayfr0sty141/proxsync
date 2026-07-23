"""Sync and browser endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import SecretBox
from app.db.models.backup import BackupHistory
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.enums import BackupStatus, GuestType, SettingsSection, UploadStatus
from app.services.settings_service import SettingsService

from .conftest import SECRET_KEY, ApiClient, StubAgentTransport

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"


async def enable_gdrive(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        await service.update_section(
            SettingsSection.GDRIVE,
            {"enabled": True, "remote_name": "gdrive", "folder": "proxsync/dump"},
        )
        await session.commit()


async def seed_backup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    filename: str = ARCHIVE,
    upload_status: UploadStatus = UploadStatus.PENDING,
) -> int:
    async with session_factory() as session:
        record = BackupHistory(
            run_id=None,
            guest_id=None,
            vmid=101,
            guest_type=GuestType.VM.value,
            guest_name="web",
            node="pve",
            storage="backup-hdd",
            filename=filename,
            size_bytes=4096,
            status=BackupStatus.SUCCESS.value,
            upload_status=upload_status.value,
            started_at=datetime.now(UTC),
        )
        session.add(record)
        await session.commit()
        return record.id


def artifact(name: str, size: int) -> dict[str, Any]:
    return {
        "filename": name,
        "path": f"/mnt/backup-hdd/dump/{name}",
        "vmid": 101,
        "guest_type": "vm",
        "size_bytes": size,
        "created_at": "2026-07-26T01:00:04Z",
        "modified_at": "2026-07-26T01:12:00Z",
        "compression": "zstd",
    }


def remote_entry(name: str, size: int) -> dict[str, Any]:
    return {
        "name": name,
        "path": name,
        "size_bytes": size,
        "is_dir": False,
        "modified_at": "2026-07-26T01:20:00Z",
        "md5": "a" * 32,
    }


class TestUploadEndpoint:
    async def test_queues_a_transfer(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)

        response = authenticated_client.post(f"/api/v1/backups/{backup_id}/upload", {})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["direction"] == "upload"
        assert body["filename"] == ARCHIVE
        assert body["remote_name"] == "gdrive"

    async def test_refuses_when_sync_is_disabled(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)

        response = authenticated_client.post(f"/api/v1/backups/{backup_id}/upload", {})

        assert response.status_code == 400
        assert "disabled" in response.json()["detail"]

    async def test_an_already_uploaded_backup_needs_force(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, upload_status=UploadStatus.UPLOADED)

        refused = authenticated_client.post(f"/api/v1/backups/{backup_id}/upload", {})
        forced = authenticated_client.post(f"/api/v1/backups/{backup_id}/upload", {"force": True})

        assert refused.status_code == 409
        assert forced.status_code == 202

    async def test_unknown_backup_is_a_404(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await enable_gdrive(session_factory)
        assert authenticated_client.post("/api/v1/backups/9999/upload", {}).status_code == 404


class TestVerifyEndpoint:
    async def test_reports_a_match(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)
        agent_transport.queue(
            200,
            {
                "filename": ARCHIVE,
                "remote": "gdrive",
                "remote_path": "proxsync/dump",
                "outcome": "match",
                "verified": True,
                "local_md5": "a" * 32,
                "remote_md5": "a" * 32,
            },
        )

        body = authenticated_client.post(f"/api/v1/backups/{backup_id}/verify", {}).json()

        assert body["verified"] is True
        assert body["outcome"] == "match"

    async def test_a_corrupted_remote_copy_is_reported(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        """The M4 acceptance criterion, end to end through the API."""
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, upload_status=UploadStatus.UPLOADED)
        agent_transport.queue(
            200,
            {
                "filename": ARCHIVE,
                "remote": "gdrive",
                "remote_path": "proxsync/dump",
                "outcome": "hash_mismatch",
                "verified": False,
                "detail": "Sizes agree but the MD5 digests differ",
            },
        )

        body = authenticated_client.post(f"/api/v1/backups/{backup_id}/verify", {}).json()

        assert body["verified"] is False
        assert body["outcome"] == "hash_mismatch"

        # The row must stop claiming it is uploaded — retention reads this.
        detail = authenticated_client.get(f"/api/v1/backups/{backup_id}").json()
        assert detail["upload_status"] == "failed"


class TestQueueEndpoints:
    async def test_status_reports_the_queue(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)
        authenticated_client.post(f"/api/v1/backups/{backup_id}/upload", {})

        body = authenticated_client.get("/api/v1/sync/status").json()

        assert body["enabled"] is True
        assert body["remote_name"] == "gdrive"
        assert body["queued"] == 1

    async def test_lists_transfers(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)
        authenticated_client.post(f"/api/v1/backups/{backup_id}/upload", {})

        body = authenticated_client.get("/api/v1/sync/tasks").json()

        assert body["total"] == 1
        assert body["items"][0]["backup_id"] == backup_id

    async def test_quota_survives_an_unreachable_remote(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        """A storage tile must not 500 because Google is briefly unavailable."""
        await enable_gdrive(session_factory)
        agent_transport.queue(502, {"detail": "rclone about failed"})

        response = authenticated_client.get("/api/v1/sync/quota")

        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["total_bytes"] is None
        assert body["detail"] is not None

    async def test_quota_says_so_when_sync_is_off(self, authenticated_client: ApiClient) -> None:
        body = authenticated_client.get("/api/v1/sync/quota").json()
        assert body["configured"] is False
        assert "disabled" in body["detail"]


class TestBrowser:
    async def test_compare_classifies_both_sides(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        await enable_gdrive(session_factory)
        agent_transport.queue(
            200, [artifact("both.vma.zst", 100), artifact("only-here.vma.zst", 2)]
        )
        agent_transport.queue(
            200,
            {
                "remote": "gdrive",
                "remote_path": "proxsync/dump",
                "entries": [
                    remote_entry("both.vma.zst", 100),
                    remote_entry("only-there.vma.zst", 3),
                ],
            },
        )

        body = authenticated_client.get("/api/v1/browser/compare").json()

        assert (body["in_sync"], body["local_only"], body["remote_only"]) == (1, 1, 1)
        assert body["detail"] is None

    async def test_local_browser_lists_the_hdd(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        await enable_gdrive(session_factory)
        agent_transport.queue(200, [artifact(ARCHIVE, 4096)])

        body = authenticated_client.get("/api/v1/browser/local").json()

        assert body["location"] == "local"
        assert body["entries"][0]["filename"] == ARCHIVE

    async def test_remote_browser_needs_sync_enabled(self, authenticated_client: ApiClient) -> None:
        assert authenticated_client.get("/api/v1/browser/remote").status_code == 400


class TestAuthorisation:
    async def test_a_viewer_cannot_start_an_upload(
        self,
        client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from app.db.models.user import User
        from app.schemas.enums import UserRole

        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)

        client.login()
        client.post(
            "/api/v1/auth/change-password",
            {"current_password": "bootstrap-password-1", "new_password": "viewer-password-5"},
        )
        client.login(password="viewer-password-5")

        async with session_factory() as session:
            admin = await session.get(User, 1)
            assert admin is not None
            admin.role = UserRole.VIEWER.value
            await session.commit()

        assert client.post(f"/api/v1/backups/{backup_id}/upload", {}).status_code == 403

    async def test_anonymous_access_is_refused(self, client: ApiClient) -> None:
        assert client.raw.get("/api/v1/sync/status").status_code == 401
