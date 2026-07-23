"""Per-guest retention decisions and physical artifact deletion.

Retention is intentionally conservative. A local candidate is removable only when both it and
the newest replacement set have completed upload (or explicitly require no upload), and every
decision is re-derived from current database state. Preview uses the same classifier but never
calls the agent and never writes an event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from app.core.errors import AgentError, AppError, NotFound
from app.core.logging import logger
from app.repositories.retention_repository import (
    RetentionBackup,
    RetentionPolicyContext,
    RetentionRepository,
)
from app.schemas.agent import AgentDeletedArtifact
from app.schemas.enums import (
    NotificationEvent,
    RetentionAction,
    SettingsSection,
    UploadStatus,
)
from app.schemas.retention import (
    RetentionDecision,
    RetentionLocation,
    RetentionPolicySnapshot,
    RetentionPreviewRequest,
    RetentionResponse,
    RetentionSummary,
)
from app.schemas.settings import GDriveSettings, RetentionSettings, SectionModel
from app.services.notification_service import NotificationSink


class RetentionAgent(Protocol):
    async def delete_backup(self, filename: str) -> AgentDeletedArtifact: ...
    async def delete_remote(
        self, *, filename: str, remote: str, remote_path: str
    ) -> dict[str, Any]: ...


class RetentionSettingsProvider(Protocol):
    async def get_section(self, section: SettingsSection) -> SectionModel: ...


@dataclass(frozen=True, slots=True)
class _PlannedDecision:
    backup: RetentionBackup
    location: RetentionLocation
    action: RetentionAction
    reason: str
    size_bytes: int
    would_delete: bool = False


@dataclass(frozen=True, slots=True)
class _PolicyResolution:
    keep_local: int
    keep_remote: int
    source: Literal["global", "job", "legacy_job", "unresolved"]
    resolved: bool = True
    block_reason: str | None = None
    run_id: int | None = None
    job_id: int | None = None


class RetentionService:
    def __init__(
        self,
        *,
        repository: RetentionRepository,
        settings_service: RetentionSettingsProvider,
        agent: RetentionAgent,
        notifications: NotificationSink | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings_service
        self._agent = agent
        self._notifications = notifications

    async def preview(self, request: RetentionPreviewRequest | None = None) -> RetentionResponse:
        """Classify current artifacts under proposed settings without any side effect."""

        proposed = request or RetentionPreviewRequest()
        policy = await self._policy(
            keep_local=proposed.keep_local,
            keep_remote=proposed.keep_remote,
            dry_run=True,
            trigger_backup_id=proposed.backup_id,
        )
        plans = await self._plans(policy, backup_id=proposed.backup_id)
        items = [self._render(plan) for plan in plans]
        return RetentionResponse(policy=policy, items=items, summary=_summarise(items))

    async def apply(self, *, backup_id: int | None = None) -> RetentionResponse:
        """Evaluate and apply the configured policy.

        A worker calls this method. In configured dry-run mode the same decisions and audit rows
        are produced, but no external delete or soft-delete marker is attempted.
        """

        initial_policy = await self._policy(
            trigger_backup_id=backup_id,
            use_frozen_run_policy=backup_id is not None,
        )
        initial_plans = await self._plans(initial_policy, backup_id=backup_id)
        completed: dict[tuple[int, RetentionLocation], RetentionDecision] = {}
        candidates: list[_PlannedDecision] = []

        for plan in initial_plans:
            key = plan.backup.id, plan.location
            if not plan.would_delete:
                item = self._render(plan)
                await self._record(plan, item, initial_policy)
                completed[key] = item
                continue

            if initial_policy.dry_run:
                item = self._render(plan)
                await self._record(
                    plan,
                    item,
                    initial_policy,
                    projected_bytes=plan.size_bytes,
                )
                completed[key] = item
                continue
            candidates.append(plan)

        # Static decisions are auditable immediately. More importantly, this releases any
        # SQLite read/write transaction before a candidate reaches the agent.
        await self._repository.commit()

        for original in candidates:
            current_policy, current = await self._revalidate_candidate(
                trigger_backup_id=backup_id,
                original=original,
            )
            key = original.backup.id, original.location
            if not current.would_delete or current_policy.dry_run:
                item = self._render(current)
                await self._record(
                    current,
                    item,
                    current_policy,
                    projected_bytes=(
                        current.size_bytes
                        if current.would_delete and current_policy.dry_run
                        else None
                    ),
                )
                await self._repository.commit()
                completed[key] = item
                continue

            # Revalidation read current copy state, lock, upload state, active restores,
            # replacement set and policy. Close that transaction immediately before I/O so
            # SQLite never holds a writer/read lock while the agent is deleting bytes.
            await self._repository.commit()
            item = await self._delete(current)
            await self._record(current, item, current_policy)
            await self._repository.commit()
            completed[key] = item

        items = [
            completed[(plan.backup.id, plan.location)]
            for plan in initial_plans
            if (plan.backup.id, plan.location) in completed
        ]
        response = RetentionResponse(
            policy=initial_policy,
            items=items,
            summary=_summarise(items),
        )
        logger.info(
            "retention_evaluated",
            trigger_backup_id=backup_id,
            evaluated=response.summary.evaluated,
            local_candidates=response.summary.local_delete_count,
            remote_candidates=response.summary.remote_delete_count,
            deleted_local=response.summary.deleted_local,
            deleted_remote=response.summary.deleted_remote,
            failed=response.summary.failed,
            dry_run=initial_policy.dry_run,
        )
        await self._notify_deleted(response)
        return response

    async def _notify_deleted(self, response: RetentionResponse) -> None:
        """Report a pass that actually removed something.

        Only real deletions: a dry run and a pass that kept everything are the normal state of
        the world, and a notification for them would be noise no one reads. Windowed rather
        than unique — retention runs repeatedly for the same guest, and each pass that frees
        space is genuinely new information once the window has elapsed.
        """
        summary = response.summary
        if self._notifications is None or not (summary.deleted_local or summary.deleted_remote):
            return

        deleted = [item for item in response.items if item.deleted]
        first = deleted[0]
        await self._notifications.enqueue(
            NotificationEvent.RETENTION_DELETED,
            dedupe_key=f"retention:{first.guest_type.value}:{first.vmid}",
            variables={
                "vmid": first.vmid,
                "guest_type": first.guest_type.value,
                "guest_name": first.guest_name,
                "deleted_local": summary.deleted_local,
                "deleted_remote": summary.deleted_remote,
                "freed_bytes": sum(item.freed_bytes for item in deleted),
                "keep_local": response.policy.keep_local,
                "keep_remote": response.policy.keep_remote,
            },
        )

    async def _policy(
        self,
        *,
        keep_local: int | None = None,
        keep_remote: int | None = None,
        dry_run: bool | None = None,
        trigger_backup_id: int | None = None,
        use_frozen_run_policy: bool = False,
    ) -> RetentionPolicySnapshot:
        retention = await self._settings.get_section(SettingsSection.RETENTION)
        gdrive = await self._settings.get_section(SettingsSection.GDRIVE)
        assert isinstance(retention, RetentionSettings)
        assert isinstance(gdrive, GDriveSettings)

        effective_keep_local = retention.keep_local if keep_local is None else keep_local
        effective_keep_remote = retention.keep_remote if keep_remote is None else keep_remote
        resolution = _PolicyResolution(
            keep_local=effective_keep_local,
            keep_remote=effective_keep_remote,
            source="global",
        )
        if use_frozen_run_policy and trigger_backup_id is not None:
            context = await self._repository.retention_policy_context(trigger_backup_id)
            resolution = _resolve_policy(
                context,
                global_keep_local=effective_keep_local,
                global_keep_remote=effective_keep_remote,
            )

        return RetentionPolicySnapshot(
            keep_local=resolution.keep_local,
            keep_remote=resolution.keep_remote,
            retention_source=resolution.source,
            policy_resolved=resolution.resolved,
            policy_block_reason=resolution.block_reason,
            policy_run_id=resolution.run_id,
            policy_job_id=resolution.job_id,
            scope=retention.scope,
            require_upload_before_delete=retention.require_upload_before_delete,
            dry_run=retention.dry_run if dry_run is None else dry_run,
            storage_warning_percent=retention.storage_warning_percent,
            storage_critical_percent=retention.storage_critical_percent,
            gdrive_enabled=gdrive.enabled,
            delete_remote_on_retention=gdrive.delete_remote_on_retention,
            remote_name=gdrive.remote_name,
            remote_folder=gdrive.folder,
            trigger_backup_id=trigger_backup_id,
        )

    async def _plans(
        self, policy: RetentionPolicySnapshot, *, backup_id: int | None
    ) -> list[_PlannedDecision]:
        if backup_id is not None:
            guest = await self._repository.guest_for_backup(backup_id)
            if guest is None:
                raise NotFound(f"No backup with id {backup_id}")
            guests = [guest]
        else:
            guests = await self._repository.guest_keys()

        plans: list[_PlannedDecision] = []
        for vmid, guest_type in guests:
            local = await self._repository.local_for_guest(vmid, guest_type)
            remote = await self._repository.remote_for_guest(vmid, guest_type)
            if not policy.policy_resolved:
                reason = policy.policy_block_reason or "retention_policy_unresolved"
                plans.extend(
                    self._blocked_plans(
                        local,
                        location=RetentionLocation.LOCAL,
                        reason=reason,
                    )
                )
                plans.extend(
                    self._blocked_plans(
                        remote,
                        location=RetentionLocation.REMOTE,
                        reason=reason,
                    )
                )
                continue
            plans.extend(await self._local_plans(local, keep=policy.keep_local))
            plans.extend(await self._remote_plans(remote, policy=policy))
        return plans

    async def _revalidate_candidate(
        self,
        *,
        trigger_backup_id: int | None,
        original: _PlannedDecision,
    ) -> tuple[RetentionPolicySnapshot, _PlannedDecision]:
        policy = await self._policy(
            trigger_backup_id=trigger_backup_id,
            use_frozen_run_policy=trigger_backup_id is not None,
        )
        plans = await self._plans(policy, backup_id=trigger_backup_id)
        current = next(
            (
                plan
                for plan in plans
                if plan.backup.id == original.backup.id and plan.location is original.location
            ),
            None,
        )
        if current is not None:
            return policy, current
        return (
            policy,
            _PlannedDecision(
                backup=original.backup,
                location=original.location,
                action=RetentionAction.SKIPPED,
                reason="candidate_copy_no_longer_present",
                size_bytes=original.size_bytes,
            ),
        )

    @staticmethod
    def _blocked_plans(
        rows: list[RetentionBackup],
        *,
        location: RetentionLocation,
        reason: str,
    ) -> list[_PlannedDecision]:
        return [
            _PlannedDecision(
                **RetentionService._base(row, location),
                action=RetentionAction.SKIPPED,
                reason=reason,
            )
            for row in rows
        ]

    async def _local_plans(
        self, rows: list[RetentionBackup], *, keep: int
    ) -> list[_PlannedDecision]:
        rankable = [row for row in rows if not row.retention_locked and row.started_at is not None]
        ranks = {row.id: index for index, row in enumerate(rankable)}
        replacements = rankable[:keep]
        plans: list[_PlannedDecision] = []

        for row in rows:
            base = self._base(row, RetentionLocation.LOCAL)
            if row.retention_locked:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="retention_locked"
                    )
                )
                continue
            if row.started_at is None:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="missing_started_at"
                    )
                )
                continue

            rank = ranks[row.id]
            if rank < keep:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.KEPT, reason=f"within_keep_local_{keep}"
                    )
                )
                continue

            candidate_block = _local_upload_blocker(row)
            if candidate_block is not None:
                plans.append(
                    _PlannedDecision(**base, action=RetentionAction.SKIPPED, reason=candidate_block)
                )
                continue

            replacement_block = next(
                (
                    _local_upload_blocker(replacement)
                    for replacement in replacements
                    if _local_upload_blocker(replacement) is not None
                ),
                None,
            )
            if replacement_block is not None:
                plans.append(
                    _PlannedDecision(
                        **base,
                        action=RetentionAction.SKIPPED,
                        reason=f"blocked_replacement_{replacement_block}",
                    )
                )
                continue

            if await self._repository.has_active_restore(row.id):
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="blocked_active_restore"
                    )
                )
                continue

            plans.append(
                _PlannedDecision(
                    **base,
                    action=RetentionAction.DELETED_LOCAL,
                    reason=f"beyond_keep_local_{keep}",
                    would_delete=True,
                )
            )

        return plans

    async def _remote_plans(
        self, rows: list[RetentionBackup], *, policy: RetentionPolicySnapshot
    ) -> list[_PlannedDecision]:
        keep = policy.keep_remote
        rankable = [row for row in rows if not row.retention_locked and row.started_at is not None]
        ranks = {row.id: index for index, row in enumerate(rankable)}
        plans: list[_PlannedDecision] = []

        for row in rows:
            base = self._base(row, RetentionLocation.REMOTE)
            if row.retention_locked:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="retention_locked"
                    )
                )
                continue
            if row.started_at is None:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="missing_started_at"
                    )
                )
                continue

            rank = ranks[row.id]
            if rank < keep:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.KEPT, reason=f"within_keep_remote_{keep}"
                    )
                )
                continue
            if not policy.delete_remote_on_retention:
                plans.append(
                    _PlannedDecision(
                        **base,
                        action=RetentionAction.SKIPPED,
                        reason="remote_deletion_disabled",
                    )
                )
                continue
            if not policy.gdrive_enabled:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="remote_sync_disabled"
                    )
                )
                continue
            if _remote_target(row) is None:
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="invalid_remote_path"
                    )
                )
                continue
            if await self._repository.has_active_restore(row.id):
                plans.append(
                    _PlannedDecision(
                        **base, action=RetentionAction.SKIPPED, reason="blocked_active_restore"
                    )
                )
                continue

            plans.append(
                _PlannedDecision(
                    **base,
                    action=RetentionAction.DELETED_REMOTE,
                    reason=f"beyond_keep_remote_{keep}",
                    would_delete=True,
                )
            )

        return plans

    @staticmethod
    def _base(backup: RetentionBackup, location: RetentionLocation) -> dict[str, Any]:
        size = (
            backup.remote_size_bytes
            if location is RetentionLocation.REMOTE and backup.remote_size_bytes is not None
            else backup.size_bytes
        )
        return {
            "backup": backup,
            "location": location,
            "size_bytes": max(size or 0, 0),
        }

    async def _delete(self, plan: _PlannedDecision) -> RetentionDecision:
        backup = plan.backup
        assert backup.filename is not None  # repository filters physical-copy queries

        try:
            if plan.location is RetentionLocation.LOCAL:
                result = await self._agent.delete_backup(backup.filename)
                freed_bytes = max(result.freed_bytes, 0)
                await self._repository.mark_local_deleted(backup.id, deleted_at=datetime.now(UTC))
            else:
                target = _remote_target(backup)
                assert target is not None  # classified before an executable plan is emitted
                remote, remote_path = target
                await self._agent.delete_remote(
                    filename=backup.filename, remote=remote, remote_path=remote_path
                )
                freed_bytes = plan.size_bytes
                await self._repository.mark_remote_deleted(backup.id, deleted_at=datetime.now(UTC))
        except AgentError as exc:
            if exc.agent_status == 404:
                if plan.location is RetentionLocation.LOCAL:
                    await self._repository.mark_local_deleted(
                        backup.id, deleted_at=datetime.now(UTC)
                    )
                else:
                    await self._repository.mark_remote_deleted(
                        backup.id, deleted_at=datetime.now(UTC)
                    )
                return self._render(
                    plan,
                    deleted=True,
                    reconciled=True,
                    freed_bytes=0,
                    reason=f"reconciled_missing_{plan.location.value}",
                )
            return self._failed(plan, exc)
        except AppError as exc:
            return self._failed(plan, exc)
        except Exception as exc:  # noqa: BLE001 - one artifact must not abort the sweep
            logger.error(
                "retention_delete_crashed",
                backup_id=backup.id,
                location=plan.location.value,
                exc_info=exc,
            )
            return self._failed(plan, exc)

        return self._render(plan, deleted=True, freed_bytes=freed_bytes)

    @staticmethod
    def _failed(plan: _PlannedDecision, error: Exception) -> RetentionDecision:
        detail = str(getattr(error, "detail", error))[:500]
        return RetentionService._render(
            plan,
            action=RetentionAction.FAILED,
            reason=f"delete_{plan.location.value}_failed:{detail}"[:255],
            error=detail,
        )

    async def _record(
        self,
        plan: _PlannedDecision,
        item: RetentionDecision,
        policy: RetentionPolicySnapshot,
        *,
        projected_bytes: int | None = None,
    ) -> None:
        snapshot = policy.model_dump(mode="json")
        snapshot.update(
            {
                "location": plan.location.value,
                "effective_keep": (
                    policy.keep_local
                    if plan.location is RetentionLocation.LOCAL
                    else policy.keep_remote
                ),
            }
        )
        await self._repository.record_event(
            backup=plan.backup,
            action=item.action,
            reason=item.reason,
            policy_snapshot=snapshot,
            freed_bytes=item.freed_bytes if projected_bytes is None else projected_bytes,
            dry_run=policy.dry_run,
        )

    @staticmethod
    def _render(
        plan: _PlannedDecision,
        *,
        action: RetentionAction | None = None,
        reason: str | None = None,
        deleted: bool = False,
        reconciled: bool = False,
        freed_bytes: int = 0,
        error: str | None = None,
    ) -> RetentionDecision:
        backup = plan.backup
        return RetentionDecision(
            backup_id=backup.id,
            vmid=backup.vmid,
            guest_type=backup.guest_type,
            guest_name=backup.guest_name,
            filename=backup.filename or "",
            location=plan.location,
            action=action or plan.action,
            reason=reason or plan.reason,
            size_bytes=plan.size_bytes,
            would_delete=plan.would_delete,
            deleted=deleted,
            reconciled=reconciled,
            freed_bytes=freed_bytes,
            error=error,
        )


def _resolve_policy(
    context: RetentionPolicyContext | None,
    *,
    global_keep_local: int,
    global_keep_remote: int,
) -> _PolicyResolution:
    if context is None:
        return _PolicyResolution(
            keep_local=global_keep_local,
            keep_remote=global_keep_remote,
            source="global",
        )

    raw = context.options or {}
    raw_source = raw.get("retention_source") if "retention_source" in raw else None
    frozen_counts = _raw_retention_counts(raw)
    linked_job_counts = _linked_job_counts(context)
    if raw_source == "global":
        return _PolicyResolution(
            keep_local=global_keep_local,
            keep_remote=global_keep_remote,
            source="global",
            run_id=context.run_id,
            job_id=context.job_id,
        )

    if raw_source == "job":
        if frozen_counts is not None:
            keep_local, keep_remote = frozen_counts
            return _PolicyResolution(
                keep_local=keep_local,
                keep_remote=keep_remote,
                source="job",
                run_id=context.run_id,
                job_id=context.job_id,
            )
        return _unresolved_policy(
            global_keep_local,
            global_keep_remote,
            reason="retention_policy_job_counts_missing",
            context=context,
        )

    if "retention_source" in raw:
        return _unresolved_policy(
            global_keep_local,
            global_keep_remote,
            reason="retention_policy_source_invalid",
            context=context,
        )

    # Legacy linked jobs predate both provenance and frozen keep fields. The surviving job
    # is the only non-destructive source of truth. Intermediate M5 rows that already contain
    # both counts can preserve them.
    if context.job_id is not None:
        counts = frozen_counts or linked_job_counts
        if counts is not None:
            keep_local, keep_remote = counts
            return _PolicyResolution(
                keep_local=keep_local,
                keep_remote=keep_remote,
                source="legacy_job",
                run_id=context.run_id,
                job_id=context.job_id,
            )
        return _unresolved_policy(
            global_keep_local,
            global_keep_remote,
            reason="retention_policy_legacy_job_counts_missing",
            context=context,
        )

    # Once a run exists, source-less + unlinked is ambiguous: it may be a true legacy manual
    # run or a legacy job Run Now whose FK was SET NULL. Deleting under a guess is unsafe.
    return _unresolved_policy(
        global_keep_local,
        global_keep_remote,
        reason="retention_policy_provenance_unknown",
        context=context,
    )


def _raw_retention_counts(options: dict[str, Any]) -> tuple[int, int] | None:
    keep_local = options.get("keep_local")
    keep_remote = options.get("keep_remote")
    if (
        type(keep_local) is not int
        or type(keep_remote) is not int
        or not 1 <= keep_local <= 365
        or not 0 <= keep_remote <= 365
    ):
        return None
    return keep_local, keep_remote


def _linked_job_counts(context: RetentionPolicyContext) -> tuple[int, int] | None:
    keep_local = context.job_keep_local
    keep_remote = context.job_keep_remote
    if (
        keep_local is None
        or keep_remote is None
        or not 1 <= keep_local <= 365
        or not 0 <= keep_remote <= 365
    ):
        return None
    return keep_local, keep_remote


def _unresolved_policy(
    global_keep_local: int,
    global_keep_remote: int,
    *,
    reason: str,
    context: RetentionPolicyContext,
) -> _PolicyResolution:
    logger.warning(
        "retention_policy_unresolved",
        run_id=context.run_id,
        job_id=context.job_id,
        trigger=context.trigger.value,
        reason=reason,
    )
    return _PolicyResolution(
        keep_local=global_keep_local,
        keep_remote=global_keep_remote,
        source="unresolved",
        resolved=False,
        block_reason=reason,
        run_id=context.run_id,
        job_id=context.job_id,
    )


def _local_upload_blocker(backup: RetentionBackup) -> str | None:
    if backup.upload_status is UploadStatus.NOT_REQUIRED:
        return None
    if backup.upload_status is UploadStatus.UPLOADED and backup.remote_path is not None:
        return None
    if backup.upload_status is UploadStatus.UPLOADED:
        return "blocked_missing_remote_metadata"
    return f"blocked_upload_{backup.upload_status.value}"


def _remote_target(backup: RetentionBackup) -> tuple[str, str] | None:
    if backup.remote_path is None:
        return None
    remote, separator, path = backup.remote_path.partition(":")
    if not separator or not remote:
        return None
    return remote, path


def _summarise(items: list[RetentionDecision]) -> RetentionSummary:
    local_candidates = [
        item for item in items if item.location is RetentionLocation.LOCAL and item.would_delete
    ]
    remote_candidates = [
        item for item in items if item.location is RetentionLocation.REMOTE and item.would_delete
    ]
    return RetentionSummary(
        evaluated=len(items),
        local_delete_count=len(local_candidates),
        local_delete_bytes=sum(item.size_bytes for item in local_candidates),
        remote_delete_count=len(remote_candidates),
        remote_delete_bytes=sum(item.size_bytes for item in remote_candidates),
        deleted_local=sum(
            item.deleted and item.location is RetentionLocation.LOCAL for item in items
        ),
        deleted_remote=sum(
            item.deleted and item.location is RetentionLocation.REMOTE for item in items
        ),
        failed=sum(item.action is RetentionAction.FAILED for item in items),
    )
