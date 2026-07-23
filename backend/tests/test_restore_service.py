"""Restore preflight, the two-phase confirmation, and the guards on both.

The interesting behaviour is everything that *refuses*: a restore that reaches the host after
one of these checks was wrong is a guest that no longer exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import SecretBox
from app.core.errors import AgentUnavailable, Conflict, NotFound, ValidationFailed
from app.db.models.backup import BackupHistory, RestoreHistory
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.agent import (
    AgentArtifact,
    AgentFilesystemUsage,
    AgentStorageEntry,
    AgentStorageStatus,
)
from app.schemas.enums import (
    BackupStatus,
    GuestStatus,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    UploadStatus,
)
from app.schemas.restore import (
    PreflightCheck,
    PreflightReport,
    RestoreConfirmRequest,
    RestoreRequest,
)
from app.services.restore_service import RestoreService
from app.services.settings_service import SettingsService

from .conftest import SECRET_KEY, make_guest, seed_guests

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"
CHECKSUM = "b" * 64
SIZE = 8 * 1024**3


class FakeRestoreAgent:
    """Answers the two read-only questions preflight asks, and can fail either of them."""

    def __init__(self) -> None:
        self.artifacts: list[AgentArtifact] = []
        self.storages: list[AgentStorageEntry] = [
            _storage("local-lvm", available=500 * 1024**3),
            _storage("backup-hdd", available=900 * 1024**3),
        ]
        self.list_error: Exception | None = None
        self.storage_error: Exception | None = None

    async def list_backups(
        self, *, vmid: int | None = None, guest_type: GuestType | None = None
    ) -> list[AgentArtifact]:
        del vmid, guest_type
        if self.list_error is not None:
            raise self.list_error
        return list(self.artifacts)

    async def storage_status(self) -> AgentStorageStatus:
        if self.storage_error is not None:
            raise self.storage_error
        return AgentStorageStatus(
            dump_root=AgentFilesystemUsage(
                path="/mnt/backup-hdd/dump",
                total_bytes=1000,
                used_bytes=100,
                free_bytes=900,
                used_percent=10.0,
            ),
            storages=list(self.storages),
        )


def _storage(name: str, *, available: int, active: bool = True) -> AgentStorageEntry:
    return AgentStorageEntry(
        name=name,
        type="lvmthin",
        active=active,
        total_bytes=available * 2,
        used_bytes=available,
        available_bytes=available,
        used_percent=50.0,
    )


def local_artifact(*, checksum: str | None = CHECKSUM) -> AgentArtifact:
    return AgentArtifact(
        filename=ARCHIVE,
        path=f"/mnt/backup-hdd/dump/{ARCHIVE}",
        vmid=101,
        guest_type=GuestType.VM,
        size_bytes=SIZE,
        created_at=datetime(2026, 7, 26, 1, tzinfo=UTC),
        modified_at=datetime(2026, 7, 26, 1, 12, tzinfo=UTC),
        compression="zstd",
        checksum_sha256=checksum,
    )


async def seed_backup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    filename: str | None = ARCHIVE,
    status: BackupStatus = BackupStatus.SUCCESS,
    checksum: str | None = CHECKSUM,
    size_bytes: int | None = SIZE,
    upload_status: UploadStatus = UploadStatus.NOT_REQUIRED,
    local_deleted: bool = False,
    remote_path: str | None = None,
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
            size_bytes=size_bytes,
            checksum_sha256=checksum,
            status=status.value,
            upload_status=upload_status.value,
            remote_path=remote_path,
            local_deleted_at=datetime.now(UTC) if local_deleted else None,
            started_at=datetime.now(UTC),
        )
        session.add(record)
        await session.commit()
        return record.id


async def seed_defaults(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        )
        await service.ensure_defaults()
        await session.commit()


def build_service(
    session: AsyncSession, agent: FakeRestoreAgent, *, ttl: int = 300
) -> RestoreService:
    return RestoreService(
        restores=SqlAlchemyRestoreRepository(session),
        history=SqlAlchemyBackupHistoryRepository(session),
        guests=SqlAlchemyGuestRepository(session),
        settings_service=SettingsService(
            repository=SqlAlchemySettingsRepository(session), secret_box=SecretBox(SECRET_KEY)
        ),
        agent=agent,
        confirmation_ttl_seconds=ttl,
    )


def check(report: PreflightReport, name: str) -> PreflightCheck:
    return next(item for item in report.checks if item.name == name)


@pytest.fixture
def agent() -> FakeRestoreAgent:
    stub = FakeRestoreAgent()
    stub.artifacts = [local_artifact()]
    return stub


class TestPreflight:
    async def test_a_clean_restore_passes_every_check(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is False
        assert report.failed == []
        assert report.source is RestoreSource.LOCAL
        assert report.target_vmid == 151
        assert report.target_type is GuestType.VM

    async def test_original_id_mode_uses_the_backups_own_vmid(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(
                    backup_id=backup_id,
                    restore_mode=RestoreMode.ORIGINAL_ID,
                    target_storage="local-lvm",
                )
            )

        assert report.target_vmid == 101

    async def test_missing_backup_is_a_404_not_a_report(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        async with session_factory() as session:
            with pytest.raises(NotFound):
                await build_service(session, agent).preflight(
                    RestoreRequest(backup_id=9999, target_vmid=151)
                )

    async def test_an_unsuccessful_backup_is_not_a_restore_source(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory, status=BackupStatus.INTERRUPTED)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is True
        assert "backup_restorable" in report.failed

    async def test_a_digest_mismatch_blocks_the_restore(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        agent.artifacts = [local_artifact(checksum="c" * 64)]

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is True
        assert "checksum_matches" in report.failed

    async def test_a_missing_digest_warns_but_does_not_block(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory, checksum=None)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is False
        assert any("No checksum was recorded" in warning for warning in report.warnings)

    async def test_a_drive_only_backup_is_restorable_and_says_so(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(
            session_factory,
            upload_status=UploadStatus.UPLOADED,
            remote_path="gdrive:proxsync/dump",
            local_deleted=True,
        )
        agent.artifacts = []

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is False
        assert report.source is RestoreSource.GDRIVE
        assert any("Google Drive" in warning for warning in report.warnings)

    async def test_an_archive_on_neither_side_blocks(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory, local_deleted=True)
        agent.artifacts = []

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is True
        assert "backup_present_locally" in report.failed

    async def test_an_unreachable_agent_blocks_rather_than_assuming_the_best(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        """A check that could not be evaluated is not a check that passed."""
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        agent.list_error = AgentUnavailable("the agent is not answering")
        agent.storage_error = AgentUnavailable("the agent is not answering")

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is True
        assert {"backup_present_locally", "storage_free_space"} <= set(report.failed)

    async def test_an_occupied_vmid_blocks_unless_overwrite_is_requested(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        await seed_guests(
            session_factory, make_guest(151, name="existing", status=GuestStatus.STOPPED)
        )

        async with session_factory() as session:
            service = build_service(session, agent)
            refused = await service.preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )
            allowed = await service.preflight(
                RestoreRequest(
                    backup_id=backup_id,
                    target_vmid=151,
                    target_storage="local-lvm",
                    overwrite_existing=True,
                )
            )

        assert "target_vmid_free" in refused.failed
        assert allowed.blocking is False
        assert any("destroyed and replaced" in warning for warning in allowed.warnings)

    async def test_a_running_target_needs_force_stop(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        await seed_guests(session_factory, make_guest(151, name="busy", status=GuestStatus.RUNNING))

        async with session_factory() as session:
            service = build_service(session, agent)
            refused = await service.preflight(
                RestoreRequest(
                    backup_id=backup_id,
                    target_vmid=151,
                    target_storage="local-lvm",
                    overwrite_existing=True,
                )
            )
            allowed = await service.preflight(
                RestoreRequest(
                    backup_id=backup_id,
                    target_vmid=151,
                    target_storage="local-lvm",
                    overwrite_existing=True,
                    force_stop=True,
                )
            )

        assert "target_guest_stopped" in refused.failed
        assert allowed.blocking is False

    async def test_a_vm_backup_cannot_replace_a_container(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        """Proxmox VMIDs are unique across VMs and containers, so this is a real collision."""
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        await seed_guests(
            session_factory,
            make_guest(151, guest_type=GuestType.LXC, name="proxy", status=GuestStatus.STOPPED),
        )

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(
                    backup_id=backup_id,
                    target_vmid=151,
                    target_storage="local-lvm",
                    overwrite_existing=True,
                    force_stop=True,
                )
            )

        assert "target_type_matches" in report.failed

    async def test_free_space_uses_the_documented_margin(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        # Exactly the archive size: enough for the bytes, short of the 15% headroom.
        agent.storages = [_storage("local-lvm", available=SIZE)]

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert "storage_free_space" in report.failed

    async def test_an_unknown_storage_blocks_and_names_the_alternatives(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="nope")
            )

        space = check(report, "storage_free_space")
        assert space.ok is False
        assert "local-lvm" in str(space.detail)

    async def test_an_unknown_size_warns_instead_of_inventing_a_requirement(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory, size_bytes=None)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is False
        assert any("size of this backup is not recorded" in item for item in report.warnings)

    async def test_another_node_is_refused_because_the_agent_cannot_reach_it(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(
                    backup_id=backup_id,
                    target_vmid=151,
                    target_storage="local-lvm",
                    target_node="pve2",
                )
            )

        assert "target_node_supported" in report.failed

    async def test_an_authorised_restore_blocks_the_next_one(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        await seed_restore(session_factory, backup_id, status=RestoreStatus.RUNNING)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert "no_restore_in_flight" in report.failed

    async def test_a_pending_request_does_not_block_another_preflight(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        """A proposal is not a restore. It blocks retention, not the next operator."""
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        await seed_restore(session_factory, backup_id, status=RestoreStatus.PENDING_CONFIRMATION)

        async with session_factory() as session:
            report = await build_service(session, agent).preflight(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm")
            )

        assert report.blocking is False


class TestCreate:
    async def test_records_the_request_and_returns_a_one_shot_token(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            record, token = await build_service(session, agent).create(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm"),
                requested_by=None,
            )
            await session.commit()
            restore_id = record.id

        assert token.startswith("rst_")
        async with session_factory() as session:
            stored = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert stored is not None
            assert stored.status == RestoreStatus.PENDING_CONFIRMATION.value
            # Only the digest is kept: a database dump must not be enough to confirm.
            assert stored.confirmation_token_hash is not None
            assert token not in str(stored.confirmation_token_hash)
            assert stored.preflight is not None

    async def test_a_blocking_report_creates_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory, status=BackupStatus.FAILED)

        async with session_factory() as session:
            with pytest.raises(ValidationFailed) as excinfo:
                await build_service(session, agent).create(
                    RestoreRequest(
                        backup_id=backup_id, target_vmid=151, target_storage="local-lvm"
                    ),
                    requested_by=None,
                )
            await session.rollback()

        assert "preflight" in excinfo.value.extra
        async with session_factory() as session:
            _, total = await build_service(session, agent).search()
        assert total == 0


class TestConfirm:
    async def test_the_happy_path_authorises_the_restore(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)

        async with session_factory() as session:
            record, token = await build_service(session, agent).create(
                RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm"),
                requested_by=None,
            )
            restore_id = record.id
            await session.commit()

        async with session_factory() as session:
            confirmed = await build_service(session, agent).confirm(
                restore_id, RestoreConfirmRequest(confirmation_token=token, target_vmid=151)
            )
            await session.commit()

        assert confirmed.status == RestoreStatus.CONFIRMED.value
        assert confirmed.confirmed_at is not None
        assert confirmed.confirmation_token_hash is None

    async def test_a_wrong_token_is_refused(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        restore_id, _ = await create_pending(session_factory, agent)

        async with session_factory() as session:
            with pytest.raises(ValidationFailed):
                await build_service(session, agent).confirm(
                    restore_id,
                    RestoreConfirmRequest(confirmation_token="rst_wrong-token", target_vmid=151),
                )

    async def test_a_wrong_vmid_is_refused_even_with_the_right_token(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        restore_id, token = await create_pending(session_factory, agent)

        async with session_factory() as session:
            with pytest.raises(ValidationFailed):
                await build_service(session, agent).confirm(
                    restore_id,
                    RestoreConfirmRequest(confirmation_token=token, target_vmid=999),
                )

    async def test_a_token_cannot_be_replayed(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        restore_id, token = await create_pending(session_factory, agent)

        async with session_factory() as session:
            await build_service(session, agent).confirm(
                restore_id, RestoreConfirmRequest(confirmation_token=token, target_vmid=151)
            )
            await session.commit()

        async with session_factory() as session:
            with pytest.raises(Conflict):
                await build_service(session, agent).confirm(
                    restore_id, RestoreConfirmRequest(confirmation_token=token, target_vmid=151)
                )

    async def test_an_expired_window_refuses_even_with_a_valid_token(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        """The refusal writes nothing — `expire_stale` owns that transition, in its own
        transaction — so the row keeps blocking retention until the sweep closes it."""
        await seed_defaults(session_factory)
        restore_id, token = await create_pending(session_factory, agent)

        async with session_factory() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert record is not None
            record.confirmation_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        async with session_factory() as session:
            with pytest.raises(Conflict):
                await build_service(session, agent).confirm(
                    restore_id, RestoreConfirmRequest(confirmation_token=token, target_vmid=151)
                )
            await session.commit()

        async with session_factory() as session:
            stored = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert stored is not None
            assert stored.status == RestoreStatus.PENDING_CONFIRMATION.value

            expired = await build_service(session, agent).expire_stale()
            await session.commit()

        assert expired == [restore_id]
        async with session_factory() as session:
            swept = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert swept is not None
            assert swept.status == RestoreStatus.EXPIRED.value

    async def test_conditions_that_changed_since_the_dialog_refuse_the_restore(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        """The report an operator agreed to is never the one the restore relies on."""
        await seed_defaults(session_factory)
        restore_id, token = await create_pending(session_factory, agent)

        agent.artifacts = []  # the archive was deleted while the dialog was open

        async with session_factory() as session:
            with pytest.raises(Conflict) as excinfo:
                await build_service(session, agent).confirm(
                    restore_id, RestoreConfirmRequest(confirmation_token=token, target_vmid=151)
                )
            await session.rollback()

        assert "backup_present_locally" in str(excinfo.value.detail)

        async with session_factory() as session:
            stored = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert stored is not None
            assert stored.status == RestoreStatus.PENDING_CONFIRMATION.value


class TestCancelAndExpiry:
    async def test_a_pending_restore_can_be_cancelled(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        restore_id, _ = await create_pending(session_factory, agent)

        async with session_factory() as session:
            record = await build_service(session, agent).cancel(restore_id)
            await session.commit()

        assert record.status == RestoreStatus.CANCELLED.value
        assert record.confirmation_token_hash is None

    async def test_a_running_restore_is_not_cancelled_here(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        backup_id = await seed_backup(session_factory)
        restore_id = await seed_restore(session_factory, backup_id, status=RestoreStatus.RUNNING)

        async with session_factory() as session:
            with pytest.raises(Conflict):
                await build_service(session, agent).cancel(restore_id)

    async def test_expire_stale_closes_abandoned_windows(
        self, session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
    ) -> None:
        await seed_defaults(session_factory)
        restore_id, _ = await create_pending(session_factory, agent)

        async with session_factory() as session:
            record = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert record is not None
            record.confirmation_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()

        async with session_factory() as session:
            expired = await build_service(session, agent).expire_stale()
            await session.commit()

        assert expired == [restore_id]
        async with session_factory() as session:
            stored = await SqlAlchemyRestoreRepository(session).get(restore_id)
            assert stored is not None
            assert stored.status == RestoreStatus.EXPIRED.value


# ---- shared fixtures ---------------------------------------------------------


async def create_pending(
    session_factory: async_sessionmaker[AsyncSession], agent: FakeRestoreAgent
) -> tuple[int, str]:
    backup_id = await seed_backup(session_factory)
    async with session_factory() as session:
        record, token = await build_service(session, agent).create(
            RestoreRequest(backup_id=backup_id, target_vmid=151, target_storage="local-lvm"),
            requested_by=None,
        )
        restore_id = record.id
        await session.commit()
    return restore_id, token


async def seed_restore(
    session_factory: async_sessionmaker[AsyncSession],
    backup_id: int,
    *,
    status: RestoreStatus,
    target_vmid: int = 161,
) -> int:
    async with session_factory() as session:
        record = RestoreHistory(
            backup_id=backup_id,
            source=RestoreSource.LOCAL.value,
            target_vmid=target_vmid,
            target_type=GuestType.VM.value,
            restore_mode=RestoreMode.NEW_ID.value,
            target_node="pve",
            target_storage="local-lvm",
            status=status.value,
            correlation_id="seeded-restore",
        )
        session.add(record)
        await session.commit()
        return record.id
