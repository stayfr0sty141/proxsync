"""Upload queueing, verification and the local/remote comparison."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.agent_client import AgentClient
from app.core.crypto import SecretBox
from app.core.errors import Conflict, ValidationFailed
from app.db.models.backup import BackupHistory
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.repositories.sync_task_repository import SqlAlchemySyncTaskRepository
from app.schemas.enums import BackupStatus, GuestType, SettingsSection, SyncStatus, UploadStatus
from app.services.settings_service import SettingsService
from app.services.sync_service import SyncService

from .conftest import SECRET_KEY

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"


async def enable_gdrive(
    session_factory: async_sessionmaker[AsyncSession], **overrides: Any
) -> None:
    async with session_factory() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        await service.update_section(
            SettingsSection.GDRIVE,
            {"enabled": True, "remote_name": "gdrive", "folder": "proxsync/dump", **overrides},
        )
        await session.commit()


async def seed_backup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    filename: str | None = ARCHIVE,
    status: BackupStatus = BackupStatus.SUCCESS,
    upload_status: UploadStatus = UploadStatus.PENDING,
    size: int = 4096,
    deleted: bool = False,
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
            size_bytes=size,
            status=status.value,
            upload_status=upload_status.value,
            started_at=datetime.now(UTC),
            local_deleted_at=datetime.now(UTC) if deleted else None,
        )
        session.add(record)
        await session.commit()
        return record.id


def build_service(session: AsyncSession, agent: AgentClient) -> SyncService:
    return SyncService(
        tasks=SqlAlchemySyncTaskRepository(session),
        history=SqlAlchemyBackupHistoryRepository(session),
        settings_service=SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        ),
        agent=agent,
    )


@pytest.fixture
def agent(settings: Any, agent_transport: Any) -> AgentClient:
    import httpx

    return AgentClient(
        settings,
        client=httpx.AsyncClient(base_url=settings.agent_base_url, transport=agent_transport),
    )


class TestQueueing:
    async def test_queues_an_upload(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            task = await build_service(session, agent).enqueue_upload(backup_id)
            await session.commit()

        assert task.status == SyncStatus.QUEUED.value
        assert task.remote_name == "gdrive"
        assert task.remote_path == "proxsync/dump"
        # upload_retry defaults to 3, so four attempts in total.
        assert task.max_attempts == 4

    async def test_refuses_when_sync_is_disabled(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            service = build_service(session, agent)
            with pytest.raises(ValidationFailed) as excinfo:
                await service.enqueue_upload(backup_id)

        assert "disabled" in excinfo.value.detail

    async def test_refuses_a_failed_backup(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, status=BackupStatus.FAILED, filename=None)

        async with session_factory() as session:
            service = build_service(session, agent)
            with pytest.raises(ValidationFailed):
                await service.enqueue_upload(backup_id)

    async def test_refuses_a_locally_deleted_artifact(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, deleted=True)

        async with session_factory() as session:
            service = build_service(session, agent)
            with pytest.raises(ValidationFailed) as excinfo:
                await service.enqueue_upload(backup_id)

        assert "nothing left to send" in excinfo.value.detail

    async def test_refuses_to_queue_the_same_backup_twice(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            await build_service(session, agent).enqueue_upload(backup_id)
            await session.commit()

        async with session_factory() as session:
            service = build_service(session, agent)
            with pytest.raises(Conflict) as excinfo:
                await service.enqueue_upload(backup_id)

        assert "already queued" in excinfo.value.detail

    async def test_an_uploaded_backup_needs_force(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, upload_status=UploadStatus.UPLOADED)

        async with session_factory() as session:
            service = build_service(session, agent)
            with pytest.raises(Conflict):
                await service.enqueue_upload(backup_id)

        async with session_factory() as session:
            task = await build_service(session, agent).enqueue_upload(backup_id, force=True)
            await session.commit()
        assert task.status == SyncStatus.QUEUED.value


class TestEnqueuePending:
    async def test_queues_every_backup_awaiting_upload(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        await seed_backup(session_factory, filename="a.vma.zst")
        await seed_backup(session_factory, filename="b.vma.zst")

        async with session_factory() as session:
            queued = await build_service(session, agent).enqueue_pending()
            await session.commit()

        assert queued == 2

    async def test_is_idempotent(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        """Called every worker cycle; a second pass must not double-queue."""
        await enable_gdrive(session_factory)
        await seed_backup(session_factory)

        async with session_factory() as session:
            await build_service(session, agent).enqueue_pending()
            await session.commit()

        async with session_factory() as session:
            second = await build_service(session, agent).enqueue_pending()
            await session.commit()

        assert second == 0

    async def test_skips_failed_and_deleted_backups(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await enable_gdrive(session_factory)
        await seed_backup(session_factory, status=BackupStatus.FAILED, filename="failed.vma.zst")
        await seed_backup(session_factory, deleted=True, filename="gone.vma.zst")

        async with session_factory() as session:
            queued = await build_service(session, agent).enqueue_pending()
            await session.commit()

        assert queued == 0

    async def test_does_nothing_while_sync_is_disabled(
        self, session_factory: async_sessionmaker[AsyncSession], agent: AgentClient
    ) -> None:
        await seed_backup(session_factory)

        async with session_factory() as session:
            assert await build_service(session, agent).enqueue_pending() == 0


class TestVerification:
    async def test_a_match_marks_the_backup_uploaded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
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
                "local_size_bytes": 4096,
                "remote_size_bytes": 4096,
                "local_md5": "a" * 32,
                "remote_md5": "a" * 32,
            },
        )

        async with session_factory() as session:
            result = await build_service(session, agent).verify_backup(backup_id)
            await session.commit()

        assert result.verified is True

        async with session_factory() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
        assert record is not None
        assert record.upload_status == UploadStatus.UPLOADED.value
        assert record.remote_checksum == "a" * 32

    async def test_a_mismatch_clears_the_uploaded_state(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
    ) -> None:
        """Retention must not delete a local artifact because of a remote copy that is bad."""
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
                "remote_size_bytes": 4096,
                "detail": "digests differ",
            },
        )

        async with session_factory() as session:
            result = await build_service(session, agent).verify_backup(backup_id)
            await session.commit()

        assert result.outcome.value == "hash_mismatch"

        async with session_factory() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
        assert record is not None
        assert record.upload_status == UploadStatus.FAILED.value

    async def test_a_missing_remote_copy_clears_the_uploaded_state(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, upload_status=UploadStatus.UPLOADED)
        agent_transport.queue(
            200,
            {
                "filename": ARCHIVE,
                "remote": "gdrive",
                "remote_path": "proxsync/dump",
                "outcome": "missing_remote",
                "verified": False,
            },
        )

        async with session_factory() as session:
            await build_service(session, agent).verify_backup(backup_id)
            await session.commit()

        async with session_factory() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
        assert record is not None
        assert record.upload_status == UploadStatus.FAILED.value

    async def test_size_match_without_a_hash_is_not_uploaded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
    ) -> None:
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory)
        agent_transport.queue(
            200,
            {
                "filename": ARCHIVE,
                "remote": "gdrive",
                "remote_path": "proxsync/dump",
                "outcome": "hash_unavailable",
                "verified": False,
                "remote_size_bytes": 4096,
            },
        )

        async with session_factory() as session:
            result = await build_service(session, agent).verify_backup(backup_id)
            await session.commit()

        assert result.verified is False

        async with session_factory() as session:
            record = await SqlAlchemyBackupHistoryRepository(session).get(backup_id)
        assert record is not None
        assert record.upload_status != UploadStatus.UPLOADED.value


class TestComparison:
    @staticmethod
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

    @staticmethod
    def remote(name: str, size: int) -> dict[str, Any]:
        return {
            "name": name,
            "path": name,
            "size_bytes": size,
            "is_dir": False,
            "modified_at": "2026-07-26T01:20:00Z",
            "md5": "a" * 32,
        }

    async def test_classifies_every_state(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
    ) -> None:
        await enable_gdrive(session_factory)
        agent_transport.queue(
            200,
            [
                self.artifact("both.vma.zst", 100),
                self.artifact("local-only.vma.zst", 200),
                self.artifact("different.vma.zst", 300),
            ],
        )
        agent_transport.queue(
            200,
            {
                "remote": "gdrive",
                "remote_path": "proxsync/dump",
                "entries": [
                    self.remote("both.vma.zst", 100),
                    self.remote("remote-only.vma.zst", 400),
                    self.remote("different.vma.zst", 999),
                ],
            },
        )

        async with session_factory() as session:
            result = await build_service(session, agent).compare()

        assert (result.in_sync, result.local_only, result.remote_only, result.size_mismatch) == (
            1,
            1,
            1,
            1,
        )
        states = {entry.filename: entry.state for entry in result.entries}
        assert states["both.vma.zst"] == "in_sync"
        assert states["local-only.vma.zst"] == "local_only"
        assert states["remote-only.vma.zst"] == "remote_only"
        assert states["different.vma.zst"] == "size_mismatch"

    async def test_an_unlistable_remote_never_reads_as_in_sync(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
    ) -> None:
        """The agent is up, but rclone cannot reach Drive. A partial answer that counted zero
        mismatches would be actively dangerous — retention acts on this."""
        await enable_gdrive(session_factory)
        agent_transport.queue(200, [self.artifact("a.vma.zst", 100)])
        agent_transport.queue(502, {"detail": "rclone about failed: token expired"})

        async with session_factory() as session:
            result = await build_service(session, agent).compare()

        assert len(result.entries) == 1
        assert result.entries[0].filename == "a.vma.zst"
        assert result.entries[0].state == "local_only"
        assert result.in_sync == 0
        assert result.local_only == 1
        assert result.detail is not None
        assert "could not be listed" in result.detail

    async def test_local_listing_includes_artifacts_proxsync_did_not_create(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent: AgentClient,
        agent_transport: Any,
    ) -> None:
        """They occupy real disk; hiding them would misrepresent the storage page."""
        await enable_gdrive(session_factory)
        backup_id = await seed_backup(session_factory, filename="known.vma.zst")
        agent_transport.queue(
            200, [self.artifact("known.vma.zst", 100), self.artifact("stranger.vma.zst", 200)]
        )

        async with session_factory() as session:
            entries = await build_service(session, agent).local_entries()

        by_name = {entry.filename: entry for entry in entries}
        assert by_name["known.vma.zst"].backup_id == backup_id
        assert by_name["stranger.vma.zst"].backup_id is None
