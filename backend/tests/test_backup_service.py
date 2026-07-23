"""Target resolution and run creation."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import SecretBox
from app.core.errors import Conflict, ValidationFailed
from app.db.models.backup import BackupJob, BackupJobTarget
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.backup_job_repository import SqlAlchemyBackupJobRepository
from app.repositories.backup_run_repository import SqlAlchemyBackupRunRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.backup import GuestTarget, ManualBackupRequest, RunOptions
from app.schemas.enums import (
    BackupMode,
    Compression,
    GuestType,
    RunStatus,
    SettingsSection,
    TargetSelector,
    TriggerType,
)
from app.services.backup_service import BackupService
from app.services.settings_service import SettingsService

from .conftest import SECRET_KEY, make_guest, seed_guests


class TestRunOptions:
    def test_legacy_persisted_options_get_retention_defaults(self) -> None:
        options = RunOptions.model_validate(
            {
                "mode": BackupMode.SNAPSHOT.value,
                "compression": Compression.ZSTD.value,
                "storage": "backup-hdd",
            }
        )

        assert options.keep_local == 2
        assert options.keep_remote == 2
        assert options.retention_source == "global"


def build_service(session: AsyncSession) -> BackupService:
    return BackupService(
        guests=SqlAlchemyGuestRepository(session),
        runs=SqlAlchemyBackupRunRepository(session),
        history=SqlAlchemyBackupHistoryRepository(session),
        settings_service=SettingsService(
            repository=SqlAlchemySettingsRepository(session),
            secret_box=SecretBox(SECRET_KEY),
        ),
    )


async def make_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    selector: TargetSelector,
    targets: list[tuple[int, GuestType]] | None = None,
    name: str = "weekly",
) -> int:
    async with session_factory() as session:
        job = BackupJob(
            name=name,
            cron_expression="0 1 * * 0",
            timezone="Asia/Jakarta",
            target_selector=selector.value,
            storage="backup-hdd",
        )
        for vmid, guest_type in targets or []:
            job.targets.append(BackupJobTarget(vmid=vmid, guest_type=guest_type.value))
        session.add(job)
        await session.commit()
        return job.id


class TestJobTargetResolution:
    async def test_all_selects_every_enabled_guest(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        job_id = await make_job(session_factory, selector=TargetSelector.ALL)

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            resolution = await build_service(session).resolve_job_targets(job)

        # 103 is in the inventory but not enabled, so it is not selected.
        assert sorted(target.vmid for target in resolution.targets) == [101, 102, 201]
        assert resolution.skipped == []

    async def test_include_selects_only_the_named_guests(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        job_id = await make_job(
            session_factory,
            selector=TargetSelector.INCLUDE,
            targets=[(101, GuestType.VM), (201, GuestType.LXC)],
        )

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            resolution = await build_service(session).resolve_job_targets(job)

        assert sorted(target.vmid for target in resolution.targets) == [101, 201]

    async def test_exclude_selects_everything_else(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        job_id = await make_job(
            session_factory,
            selector=TargetSelector.EXCLUDE,
            targets=[(101, GuestType.VM)],
        )

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            resolution = await build_service(session).resolve_job_targets(job)

        assert sorted(target.vmid for target in resolution.targets) == [102, 201]

    async def test_a_named_target_that_cannot_run_is_reported_not_silently_dropped(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        """A schedule that quietly backs up nothing looks identical to a healthy one."""
        del seeded_guests
        job_id = await make_job(
            session_factory,
            selector=TargetSelector.INCLUDE,
            targets=[(101, GuestType.VM), (103, GuestType.VM), (999, GuestType.VM)],
        )

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            resolution = await build_service(session).resolve_job_targets(job)

        assert [target.vmid for target in resolution.targets] == [101]
        assert len(resolution.skipped) == 2
        assert any("not enabled for backup" in reason for reason in resolution.skipped)
        assert any("no longer in the inventory" in reason for reason in resolution.skipped)

    async def test_vm_and_lxc_with_the_same_vmid_are_distinct_targets(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_guests(
            session_factory,
            make_guest(300, name="a-vm", node="pve"),
            make_guest(300, guest_type=GuestType.LXC, name="a-container", node="pve2"),
        )
        job_id = await make_job(
            session_factory,
            selector=TargetSelector.INCLUDE,
            targets=[(300, GuestType.LXC)],
        )

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            resolution = await build_service(session).resolve_job_targets(job)

        assert [(t.vmid, t.guest_type) for t in resolution.targets] == [(300, GuestType.LXC)]


class TestManualTargets:
    async def test_resolves_enabled_guests(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        async with session_factory() as session:
            resolved = await build_service(session).resolve_explicit_targets(
                [GuestTarget(vmid=101, guest_type=GuestType.VM)]
            )
        assert resolved[0].guest_name == "web"

    async def test_the_allow_list_applies_to_manual_backups_too(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        """Otherwise the allow-list is decorative: anyone could copy a guest policy forbids."""
        del seeded_guests
        async with session_factory() as session:
            with pytest.raises(ValidationFailed) as excinfo:
                await build_service(session).resolve_explicit_targets(
                    [GuestTarget(vmid=103, guest_type=GuestType.VM)]
                )

        problems = excinfo.value.extra["problems"]
        assert "not enabled for backup" in problems[0]
        assert "Guests page" in problems[0]

    async def test_one_bad_target_refuses_the_whole_request(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        """A partial backup that silently dropped a guest is worse than a 400."""
        del seeded_guests
        async with session_factory() as session:
            with pytest.raises(ValidationFailed) as excinfo:
                await build_service(session).resolve_explicit_targets(
                    [
                        GuestTarget(vmid=101, guest_type=GuestType.VM),
                        GuestTarget(vmid=999, guest_type=GuestType.VM),
                    ]
                )
        assert len(excinfo.value.extra["problems"]) == 1

    async def test_unknown_guest_names_itself_in_the_error(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        async with session_factory() as session:
            with pytest.raises(ValidationFailed) as excinfo:
                await build_service(session).resolve_explicit_targets(
                    [GuestTarget(vmid=777, guest_type=GuestType.LXC)]
                )
        assert "lxc 777" in excinfo.value.extra["problems"][0]


class TestRunCreation:
    async def test_a_manual_run_records_its_plan_and_options(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        async with session_factory() as session:
            settings_service = SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=SecretBox(SECRET_KEY),
            )
            await settings_service.ensure_defaults()
            await settings_service.update_section(
                SettingsSection.RETENTION, {"keep_local": 5, "keep_remote": 3}
            )
            run = await build_service(session).request_manual_run(
                ManualBackupRequest(
                    targets=[
                        GuestTarget(vmid=101, guest_type=GuestType.VM),
                        GuestTarget(vmid=201, guest_type=GuestType.LXC),
                    ],
                    mode=BackupMode.STOP,
                ),
                # No user row exists in this test, and `requested_by` is a real foreign key.
                requested_by=None,
            )
            await session.commit()
            run_id = run.id

        async with session_factory() as session:
            stored = await SqlAlchemyBackupRunRepository(session).get(run_id)

        assert stored is not None
        assert stored.status == RunStatus.QUEUED.value
        assert stored.trigger == TriggerType.MANUAL.value
        assert stored.guest_total == 2
        assert [target["vmid"] for target in stored.targets or []] == [101, 201]
        assert stored.options is not None
        # The request's override wins; everything else falls back to the configured default.
        assert stored.options["mode"] == BackupMode.STOP.value
        assert stored.options["compression"] == Compression.ZSTD.value
        assert stored.options["storage"] == "backup-hdd"
        assert stored.options["keep_local"] == 5
        assert stored.options["keep_remote"] == 3
        assert stored.options["retention_source"] == "global"

    async def test_a_job_run_freezes_the_jobs_settings(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        job_id = await make_job(session_factory, selector=TargetSelector.ALL)

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            job.compression = Compression.GZIP.value
            job.keep_local = 7
            job.keep_remote = 4
            run = await build_service(session).request_job_run(
                job, trigger=TriggerType.SCHEDULE, requested_by=None
            )
            await session.commit()
            run_id, options = run.id, run.options

        assert options is not None
        assert options["compression"] == Compression.GZIP.value
        assert options["keep_local"] == 7
        assert options["keep_remote"] == 4
        assert options["retention_source"] == "job"
        assert options["notes"] == "proxsync job 'weekly'"

        # Editing the job afterwards must not change a run already queued.
        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            job.compression = Compression.LZO.value
            job.keep_local = 1
            job.keep_remote = 0
            await session.commit()

        async with session_factory() as session:
            stored = await SqlAlchemyBackupRunRepository(session).get(run_id)
        assert stored is not None
        assert (stored.options or {})["compression"] == Compression.GZIP.value
        assert (stored.options or {})["keep_local"] == 7
        assert (stored.options or {})["keep_remote"] == 4

    async def test_a_job_resolving_to_nothing_is_refused_with_the_reason(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        job_id = await make_job(
            session_factory,
            selector=TargetSelector.INCLUDE,
            targets=[(103, GuestType.VM)],
        )

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            with pytest.raises(ValidationFailed) as excinfo:
                await build_service(session).request_job_run(
                    job, trigger=TriggerType.SCHEDULE, requested_by=None
                )

        assert "resolves to no guests" in excinfo.value.detail
        assert excinfo.value.extra["skipped"]

    async def test_a_job_cannot_overlap_its_own_previous_run(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        """A weekly backup still running when the next one fires must not start twice."""
        del seeded_guests
        job_id = await make_job(session_factory, selector=TargetSelector.ALL)

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            await build_service(session).request_job_run(
                job, trigger=TriggerType.SCHEDULE, requested_by=None
            )
            await session.commit()

        async with session_factory() as session:
            job = await SqlAlchemyBackupJobRepository(session).get(job_id)
            assert job is not None
            with pytest.raises(Conflict) as excinfo:
                await build_service(session).request_job_run(
                    job, trigger=TriggerType.SCHEDULE, requested_by=None
                )

        assert "already has run" in excinfo.value.detail

    async def test_upload_follows_the_gdrive_setting_when_not_overridden(
        self, session_factory: async_sessionmaker[AsyncSession], seeded_guests: object
    ) -> None:
        del seeded_guests
        async with session_factory() as session:
            service = SettingsService(
                repository=SqlAlchemySettingsRepository(session),
                secret_box=SecretBox(SECRET_KEY),
            )
            await service.ensure_defaults()
            await service.update_section(SettingsSection.GDRIVE, {"enabled": True})
            run = await build_service(session).request_manual_run(
                ManualBackupRequest(targets=[GuestTarget(vmid=101, guest_type=GuestType.VM)]),
                requested_by=None,
            )
            await session.commit()
            options = run.options

        assert options is not None
        assert options["upload"] is True
