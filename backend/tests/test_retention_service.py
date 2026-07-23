"""Retention policy ordering, safety gates, deletion, and audit semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import SecretBox
from app.core.errors import AgentError
from app.db.models.backup import (
    BackupHistory,
    BackupJob,
    BackupRun,
    RestoreHistory,
    RetentionEvent,
)
from app.repositories.retention_repository import SqlAlchemyRetentionRepository
from app.repositories.settings_repository import SqlAlchemySettingsRepository
from app.schemas.agent import AgentDeletedArtifact
from app.schemas.backup import RunOptions
from app.schemas.enums import (
    BackupMode,
    BackupStatus,
    Compression,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    RetentionAction,
    RunStatus,
    SettingsSection,
    TriggerType,
    UploadStatus,
)
from app.schemas.retention import RetentionLocation, RetentionPreviewRequest
from app.schemas.settings import RetentionSettings
from app.services.retention_service import RetentionService
from app.services.settings_service import SettingsService

from .conftest import SECRET_KEY

NOW = datetime(2026, 7, 23, 1, tzinfo=UTC)


class FakeRetentionAgent:
    def __init__(self) -> None:
        self.local_calls: list[str] = []
        self.remote_calls: list[tuple[str, str, str]] = []
        self.missing_local: set[str] = set()
        self.missing_remote: set[str] = set()

    async def delete_backup(self, filename: str) -> AgentDeletedArtifact:
        self.local_calls.append(filename)
        if filename in self.missing_local:
            raise AgentError("artifact is already absent", agent_status=404)
        return AgentDeletedArtifact(
            filename=filename,
            deleted=[f"/dump/{filename}"],
            freed_bytes=1_000,
        )

    async def delete_remote(
        self, *, filename: str, remote: str, remote_path: str
    ) -> dict[str, Any]:
        self.remote_calls.append((filename, remote, remote_path))
        if filename in self.missing_remote:
            raise AgentError("remote object is already absent", agent_status=404)
        return {"deleted": True}


class LockBeforeRevalidationRepository(SqlAlchemyRetentionRepository):
    def __init__(
        self,
        session: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        candidate_id: int,
    ) -> None:
        super().__init__(session)
        self._session_factory = session_factory
        self._candidate_id = candidate_id
        self._injected = False

    async def commit(self) -> None:
        await super().commit()
        if self._injected:
            return
        self._injected = True
        async with self._session_factory() as session:
            candidate = await session.get(BackupHistory, self._candidate_id)
            assert candidate is not None
            candidate.retention_locked = True
            await session.commit()


class DatabaseWritingAgent(FakeRetentionAgent):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._session_factory = session_factory

    async def delete_backup(self, filename: str) -> AgentDeletedArtifact:
        async with self._session_factory() as session:
            session.add(
                RetentionEvent(
                    backup_id=None,
                    vmid=999,
                    guest_type=GuestType.VM.value,
                    action=RetentionAction.EVALUATED.value,
                    reason="agent-side-concurrent-write-probe",
                    policy_snapshot=None,
                    dry_run=False,
                )
            )
            await session.commit()
        return await super().delete_backup(filename)


def backup(
    index: int,
    *,
    vmid: int = 101,
    upload: UploadStatus = UploadStatus.NOT_REQUIRED,
    remote: bool = False,
    locked: bool = False,
    run_id: int | None = None,
    started_at: datetime | None = NOW,
) -> BackupHistory:
    filename = f"vzdump-qemu-{vmid}-retention-{index}.vma.zst"
    return BackupHistory(
        run_id=run_id,
        guest_id=None,
        vmid=vmid,
        guest_type=GuestType.VM.value,
        guest_name=f"guest-{vmid}",
        node="pve",
        storage="backup-hdd",
        filename=filename,
        local_path=f"/dump/{filename}",
        size_bytes=index * 100,
        status=BackupStatus.SUCCESS.value,
        upload_status=upload.value,
        uploaded_at=NOW if upload is UploadStatus.UPLOADED else None,
        remote_path="gdrive:proxsync/dump" if remote else None,
        remote_size_bytes=index * 110 if remote else None,
        retention_locked=locked,
        started_at=started_at,
        finished_at=started_at,
        correlation_id=f"backup-{vmid}-{index}",
    )


async def configure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    keep_local: int = 2,
    keep_remote: int = 2,
    dry_run: bool = False,
    gdrive_enabled: bool = False,
    delete_remote: bool = True,
) -> None:
    async with session_factory() as session:
        service = SettingsService(
            repository=SqlAlchemySettingsRepository(session),
            secret_box=SecretBox(SECRET_KEY),
        )
        await service.ensure_defaults()
        await service.update_section(
            SettingsSection.RETENTION,
            {
                "keep_local": keep_local,
                "keep_remote": keep_remote,
                "dry_run": dry_run,
            },
        )
        await service.update_section(
            SettingsSection.GDRIVE,
            {
                "enabled": gdrive_enabled,
                "delete_remote_on_retention": delete_remote,
            },
        )
        await session.commit()


def service(session: AsyncSession, agent: FakeRetentionAgent) -> RetentionService:
    return RetentionService(
        repository=SqlAlchemyRetentionRepository(session),
        settings_service=SettingsService(
            repository=SqlAlchemySettingsRepository(session),
            secret_box=SecretBox(SECRET_KEY),
        ),
        agent=agent,
    )


async def add_rows(
    session_factory: async_sessionmaker[AsyncSession], rows: list[BackupHistory]
) -> list[int]:
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
        return [row.id for row in rows]


class TestRetentionSelection:
    async def test_is_per_guest_and_uses_stable_timestamp_id_order(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(
            session_factory,
            keep_local=2,
            keep_remote=5,
            gdrive_enabled=True,
        )
        guest_a = [
            backup(index, upload=UploadStatus.UPLOADED, remote=True) for index in range(1, 6)
        ]
        guest_b = [backup(index, vmid=202) for index in range(10, 13)]
        await add_rows(session_factory, [*guest_a, *guest_b])
        agent = FakeRetentionAgent()

        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=guest_a[-1].id)
            await session.commit()

        # All timestamps tie. The newest ids own the two slots and deletion remains stable.
        assert agent.local_calls == [
            guest_a[2].filename,
            guest_a[1].filename,
            guest_a[0].filename,
        ]
        assert result.summary.local_delete_count == 3
        assert result.summary.remote_delete_count == 0
        assert {item.vmid for item in result.items} == {101}

        async with session_factory() as session:
            events = list((await session.execute(select(RetentionEvent))).scalars())
            untouched = list(
                (
                    await session.execute(select(BackupHistory).where(BackupHistory.vmid == 202))
                ).scalars()
            )
        assert len(events) == 10  # one local and one actual-remote decision per row
        assert all(row.local_deleted_at is None for row in untouched)
        snapshot = events[0].policy_snapshot
        assert snapshot is not None
        assert snapshot["require_upload_before_delete"] is True
        assert snapshot["scope"] == "per_guest"
        assert snapshot["remote_name"] == "gdrive"
        assert {"location", "effective_keep"} <= snapshot.keys()

    async def test_locked_rows_do_not_consume_slots_and_active_restore_blocks(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        oldest = backup(1)
        replacement = backup(2)
        pinned = backup(3, locked=True)
        await add_rows(session_factory, [oldest, replacement, pinned])
        async with session_factory() as session:
            session.add(
                RestoreHistory(
                    backup_id=oldest.id,
                    source=RestoreSource.LOCAL.value,
                    target_vmid=901,
                    target_type=GuestType.VM.value,
                    restore_mode=RestoreMode.NEW_ID.value,
                    target_node="pve",
                    target_storage="local-lvm",
                    status=RestoreStatus.CONFIRMED.value,
                    correlation_id="restore-active",
                )
            )
            await session.commit()
        agent = FakeRetentionAgent()

        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=replacement.id)
            await session.commit()

        local = {
            item.backup_id: item
            for item in result.items
            if item.location is RetentionLocation.LOCAL
        }
        assert local[pinned.id].reason == "retention_locked"
        assert local[replacement.id].action is RetentionAction.KEPT
        assert local[oldest.id].reason == "blocked_active_restore"
        assert agent.local_calls == []

    async def test_candidate_and_newest_replacement_must_be_upload_safe(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        replacement_pending = backup(2, vmid=101, upload=UploadStatus.PENDING)
        older_ready = backup(1, vmid=101)
        replacement_ready = backup(4, vmid=202)
        older_pending = backup(3, vmid=202, upload=UploadStatus.PENDING)
        await add_rows(
            session_factory,
            [older_ready, replacement_pending, older_pending, replacement_ready],
        )
        agent = FakeRetentionAgent()

        async with session_factory() as session:
            first = await service(session, agent).apply(backup_id=older_ready.id)
            second = await service(session, agent).apply(backup_id=older_pending.id)
            await session.commit()

        first_old = next(
            item
            for item in first.items
            if item.backup_id == older_ready.id and item.location is RetentionLocation.LOCAL
        )
        second_old = next(
            item
            for item in second.items
            if item.backup_id == older_pending.id and item.location is RetentionLocation.LOCAL
        )
        assert first_old.reason == "blocked_replacement_blocked_upload_pending"
        assert second_old.reason == "blocked_upload_pending"
        assert agent.local_calls == []


class TestRetentionExecution:
    async def test_trigger_uses_frozen_run_policy_while_preview_uses_proposal(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(
            session_factory,
            keep_local=4,
            keep_remote=5,
            gdrive_enabled=True,
        )
        async with session_factory() as session:
            job = BackupJob(name="nightly-frozen-policy")
            session.add(job)
            await session.flush()
            frozen_options = RunOptions(
                mode=BackupMode.SNAPSHOT,
                compression=Compression.ZSTD,
                storage="backup-hdd",
                upload=True,
                retention_source="job",
                keep_local=1,
                keep_remote=5,
            ).model_dump(mode="json")
            run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="scheduled-run",
                options=frozen_options,
            )
            manual_run = BackupRun(
                trigger=TriggerType.MANUAL.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="manual-run",
                options={**frozen_options, "retention_source": "global"},
            )
            session.add_all([run, manual_run])
            await session.flush()
            rows = [
                backup(
                    index,
                    run_id=run.id,
                    upload=UploadStatus.UPLOADED,
                    remote=True,
                )
                for index in range(1, 4)
            ]
            manual_rows = [
                backup(
                    index + 10,
                    vmid=202,
                    run_id=manual_run.id,
                    upload=UploadStatus.UPLOADED,
                    remote=True,
                )
                for index in range(1, 4)
            ]
            session.add_all([*rows, *manual_rows])
            await session.commit()
        agent = FakeRetentionAgent()

        async with session_factory() as session:
            preview = await service(session, agent).preview(
                RetentionPreviewRequest(
                    backup_id=rows[-1].id,
                    keep_local=3,
                    keep_remote=5,
                )
            )
            event_count = len(list((await session.execute(select(RetentionEvent))).scalars()))
            result = await service(session, agent).apply(backup_id=rows[-1].id)
            manual_result = await service(session, agent).apply(backup_id=manual_rows[-1].id)
            await session.commit()

        assert preview.policy.keep_local == 3
        assert preview.summary.local_delete_count == 0
        assert event_count == 0
        assert agent.local_calls == [rows[1].filename, rows[0].filename]
        assert result.policy.keep_local == 1
        assert manual_result.policy.keep_local == 4
        assert manual_result.summary.local_delete_count == 0
        assert all(item.action is not RetentionAction.FAILED for item in result.items)

    async def test_deleted_job_keeps_explicit_frozen_policy(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        async with session_factory() as session:
            job = BackupJob(name="deleted-policy-owner", keep_local=3, keep_remote=0)
            session.add(job)
            await session.flush()
            run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="deleted-job-run",
                options=RunOptions(
                    mode=BackupMode.SNAPSHOT,
                    compression=Compression.ZSTD,
                    storage="backup-hdd",
                    retention_source="job",
                    keep_local=3,
                    keep_remote=0,
                ).model_dump(mode="json"),
            )
            session.add(run)
            await session.flush()
            rows = [backup(index + 20, run_id=run.id) for index in range(1, 5)]
            session.add_all(rows)
            await session.commit()
            job_id = job.id

        async with session_factory() as session:
            stored_job = await session.get(BackupJob, job_id)
            assert stored_job is not None
            await session.delete(stored_job)
            await session.commit()

        agent = FakeRetentionAgent()
        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)

        assert result.policy.retention_source == "job"
        assert result.policy.policy_job_id is None
        assert result.policy.keep_local == 3
        assert agent.local_calls == [rows[0].filename]

    async def test_legacy_linked_job_uses_current_job_counts_not_schema_defaults(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=2, keep_remote=0)
        async with session_factory() as session:
            job = BackupJob(name="pre-m5-policy", keep_local=5, keep_remote=0)
            session.add(job)
            await session.flush()
            run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="legacy-linked-run",
                options={
                    "mode": BackupMode.SNAPSHOT.value,
                    "compression": Compression.ZSTD.value,
                    "storage": "backup-hdd",
                },
            )
            session.add(run)
            await session.flush()
            rows = [backup(index + 30, run_id=run.id) for index in range(1, 7)]
            session.add_all(rows)
            await session.commit()

        agent = FakeRetentionAgent()
        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)

        assert result.policy.retention_source == "legacy_job"
        assert result.policy.keep_local == 5
        assert agent.local_calls == [rows[0].filename]

    async def test_unlinked_legacy_run_fails_closed_and_audits_skips(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        async with session_factory() as session:
            # `manual` is deliberately ambiguous: legacy job Run Now used the same trigger.
            run = BackupRun(
                trigger=TriggerType.MANUAL.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="orphaned-legacy-run",
                options={
                    "mode": BackupMode.SNAPSHOT.value,
                    "compression": Compression.ZSTD.value,
                    "storage": "backup-hdd",
                },
            )
            session.add(run)
            await session.flush()
            rows = [backup(index + 40, run_id=run.id) for index in range(1, 4)]
            session.add_all(rows)
            await session.commit()

        agent = FakeRetentionAgent()
        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)

        assert result.policy.policy_resolved is False
        assert result.policy.retention_source == "unresolved"
        assert result.policy.policy_block_reason == "retention_policy_provenance_unknown"
        assert agent.local_calls == []
        assert all(
            item.action is RetentionAction.SKIPPED
            and item.reason == "retention_policy_provenance_unknown"
            for item in result.items
        )
        async with session_factory() as session:
            events = list(
                (
                    await session.execute(
                        select(RetentionEvent).where(
                            RetentionEvent.backup_id.in_([row.id for row in rows])
                        )
                    )
                ).scalars()
            )
        assert len(events) == 3
        assert all(event.action == RetentionAction.SKIPPED.value for event in events)

    async def test_explicit_job_source_without_frozen_counts_fails_closed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        async with session_factory() as session:
            job = BackupJob(name="malformed-explicit-policy", keep_local=1)
            session.add(job)
            await session.flush()
            run = BackupRun(
                job_id=job.id,
                trigger=TriggerType.SCHEDULE.value,
                status=RunStatus.SUCCESS.value,
                correlation_id="malformed-explicit-run",
                options={
                    "mode": BackupMode.SNAPSHOT.value,
                    "compression": Compression.ZSTD.value,
                    "storage": "backup-hdd",
                    "retention_source": "job",
                },
            )
            session.add(run)
            await session.flush()
            rows = [backup(75, run_id=run.id), backup(76, run_id=run.id)]
            session.add_all(rows)
            await session.commit()

        agent = FakeRetentionAgent()
        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)

        assert result.policy.policy_resolved is False
        assert result.policy.policy_block_reason == "retention_policy_job_counts_missing"
        assert agent.local_calls == []

    async def test_candidate_is_reloaded_and_revalidated_before_delete(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        rows = [backup(81), backup(82)]
        await add_rows(session_factory, rows)
        agent = FakeRetentionAgent()

        async with session_factory() as session:
            retention = RetentionService(
                repository=LockBeforeRevalidationRepository(
                    session,
                    session_factory=session_factory,
                    candidate_id=rows[0].id,
                ),
                settings_service=SettingsService(
                    repository=SqlAlchemySettingsRepository(session),
                    secret_box=SecretBox(SECRET_KEY),
                ),
                agent=agent,
            )
            result = await retention.apply(backup_id=rows[-1].id)

        candidate = next(item for item in result.items if item.backup_id == rows[0].id)
        assert candidate.action is RetentionAction.SKIPPED
        assert candidate.reason == "retention_locked"
        assert agent.local_calls == []

    async def test_agent_io_runs_without_holding_sqlite_writer_transaction(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        rows = [backup(91), backup(92)]
        await add_rows(session_factory, rows)
        agent = DatabaseWritingAgent(session_factory)

        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)

        assert result.summary.deleted_local == 1
        async with session_factory() as session:
            probe = (
                await session.execute(
                    select(RetentionEvent).where(
                        RetentionEvent.reason == "agent-side-concurrent-write-probe"
                    )
                )
            ).scalar_one()
        assert probe.vmid == 999

    async def test_dry_run_records_projected_events_without_side_effects(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0, dry_run=True)
        rows = [backup(1), backup(2)]
        await add_rows(session_factory, rows)
        agent = FakeRetentionAgent()

        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)
            await session.commit()

        candidate = next(item for item in result.items if item.backup_id == rows[0].id)
        assert candidate.would_delete is True
        assert candidate.deleted is False
        assert candidate.action is RetentionAction.DELETED_LOCAL
        assert agent.local_calls == []
        async with session_factory() as session:
            stored = await session.get(BackupHistory, rows[0].id)
            event = (
                await session.execute(
                    select(RetentionEvent).where(
                        RetentionEvent.backup_id == rows[0].id,
                        RetentionEvent.action == RetentionAction.DELETED_LOCAL.value,
                    )
                )
            ).scalar_one()
        assert stored is not None and stored.local_deleted_at is None
        assert event.dry_run is True
        assert event.freed_bytes == rows[0].size_bytes

    async def test_404_retry_reconciles_marker_as_success(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(session_factory, keep_local=1, keep_remote=0)
        rows = [backup(1), backup(2)]
        await add_rows(session_factory, rows)
        agent = FakeRetentionAgent()
        assert rows[0].filename is not None
        agent.missing_local.add(rows[0].filename)

        async with session_factory() as session:
            result = await service(session, agent).apply(backup_id=rows[-1].id)
            await session.commit()

        candidate = next(item for item in result.items if item.backup_id == rows[0].id)
        assert candidate.deleted is True
        assert candidate.reconciled is True
        assert candidate.reason == "reconciled_missing_local"
        async with session_factory() as session:
            stored = await session.get(BackupHistory, rows[0].id)
        assert stored is not None and stored.local_deleted_at is not None
        assert stored.status == BackupStatus.DELETED.value

    async def test_local_and_remote_copies_have_independent_lifecycle(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await configure(
            session_factory,
            keep_local=2,
            keep_remote=1,
            gdrive_enabled=True,
        )
        rows = [backup(index, upload=UploadStatus.UPLOADED, remote=True) for index in range(1, 3)]
        await add_rows(session_factory, rows)
        agent = FakeRetentionAgent()
        assert rows[0].filename is not None
        agent.missing_remote.add(rows[0].filename)

        async with session_factory() as session:
            first = await service(session, agent).apply(backup_id=rows[-1].id)
            await session.commit()

        assert first.summary.deleted_local == 0
        assert first.summary.deleted_remote == 1
        remote_candidate = next(
            item
            for item in first.items
            if item.backup_id == rows[0].id and item.location is RetentionLocation.REMOTE
        )
        assert remote_candidate.reconciled is True
        async with session_factory() as session:
            oldest = await session.get(BackupHistory, rows[0].id)
        assert oldest is not None
        assert oldest.remote_deleted_at is not None
        assert oldest.local_deleted_at is None
        assert oldest.status == BackupStatus.SUCCESS.value

        await configure(
            session_factory,
            keep_local=1,
            keep_remote=0,
            gdrive_enabled=True,
        )
        async with session_factory() as session:
            second = await service(session, agent).apply(backup_id=rows[-1].id)
            await session.commit()

        assert second.summary.deleted_local == 1
        async with session_factory() as session:
            oldest = await session.get(BackupHistory, rows[0].id)
            newest = await session.get(BackupHistory, rows[1].id)
        assert oldest is not None and oldest.status == BackupStatus.DELETED.value
        assert newest is not None and newest.status == BackupStatus.SUCCESS.value


def test_upload_before_delete_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError):
        RetentionSettings.model_validate({"require_upload_before_delete": False})
