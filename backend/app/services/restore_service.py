"""Restore orchestration: the guards, the two-phase confirmation, and the record of both.

A restore is the only operation ProxSync performs that destroys data. Everything here follows
from that:

**Nothing runs on the first request.** `POST /restores` produces a `pending_confirmation` row
and a short-lived token. The restore starts only when a second request echoes that token *and*
the target VMID, which is the difference between a click and a decision.

**Preflight is run twice, and the second one decides.** The report shown in the dialog can be
five minutes old by the time anyone confirms it: the archive may have been deleted, the target
guest started, another restore authorised. Confirmation re-runs the same checks against the
live host and refuses if anything now blocks, so the report an operator agreed to is never the
one the restore relies on.

**A blocking check is a refusal, not a warning.** The agent enforces its own floor — digest,
guest type, overwrite, running target — and these checks sit above it. Where a check cannot be
*evaluated* (the agent is unreachable, the storage is not listed) it fails: a restore that
cannot be verified as safe is not a restore that may proceed.

This service decides and records. It never runs a restore — `app.workers.restore_worker` does,
because `qmrestore` takes far longer than an HTTP request may.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.errors import AgentError, AgentUnavailable, Conflict, NotFound, ValidationFailed
from app.core.logging import get_correlation_id, logger
from app.db.models.backup import BackupHistory, RestoreHistory
from app.db.models.guest import Guest
from app.repositories.backup_history_repository import SqlAlchemyBackupHistoryRepository
from app.repositories.guest_repository import SqlAlchemyGuestRepository
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.schemas.agent import AgentArtifact, AgentStorageStatus
from app.schemas.enums import (
    BackupStatus,
    GuestStatus,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    SettingsSection,
    UploadStatus,
)
from app.schemas.restore import (
    CONFIRMATION_TOKEN_PREFIX,
    PreflightCheck,
    PreflightReport,
    RestoreConfirmRequest,
    RestoreRequest,
)
from app.schemas.settings import AgentSettings, ProxmoxSettings
from app.services.settings_service import SettingsService

FREE_SPACE_MARGIN = 1.15
"""A restored guest needs more room than its compressed archive occupies, and the margin has
to cover the difference plus whatever the storage needs while writing. 15% is the figure
`docs/ARCHITECTURE.md` §6 specifies; it is a guard against the obvious failure, not a
prediction of the restored size."""

DEFAULT_CONFIRMATION_TTL_SECONDS = 300


class RestoreAgent(Protocol):
    """The two things preflight asks the host. Deliberately read-only — nothing this service
    calls can change anything, which is what makes a preflight safe to run at any time."""

    async def list_backups(
        self, *, vmid: int | None = None, guest_type: GuestType | None = None
    ) -> list[AgentArtifact]: ...
    async def storage_status(self) -> AgentStorageStatus: ...


class RestoreService:
    def __init__(
        self,
        *,
        restores: SqlAlchemyRestoreRepository,
        history: SqlAlchemyBackupHistoryRepository,
        guests: SqlAlchemyGuestRepository,
        settings_service: SettingsService,
        agent: RestoreAgent,
        confirmation_ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
    ) -> None:
        self._restores = restores
        self._history = history
        self._guests = guests
        self._settings_service = settings_service
        self._agent = agent
        self._ttl_seconds = confirmation_ttl_seconds

    # ---- reads ---------------------------------------------------------------

    async def get(self, restore_id: int) -> RestoreHistory:
        record = await self._restores.get(restore_id)
        if record is None:
            raise NotFound(f"No restore with id {restore_id}")
        return record

    async def source_backup(self, record: RestoreHistory) -> BackupHistory | None:
        return await self._history.get(record.backup_id)

    async def search(
        self,
        *,
        status: RestoreStatus | None = None,
        backup_id: int | None = None,
        target_vmid: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RestoreHistory], int]:
        rows = await self._restores.search(
            status=status,
            backup_id=backup_id,
            target_vmid=target_vmid,
            limit=limit,
            offset=offset,
        )
        total = await self._restores.count(
            status=status, backup_id=backup_id, target_vmid=target_vmid
        )
        return rows, total

    # ---- preflight -----------------------------------------------------------

    async def preflight(self, request: RestoreRequest) -> PreflightReport:
        """Answer "would this restore work?" without writing anything.

        Every branch appends a check rather than raising, so the caller always receives a
        complete report. The single exception is a backup id that does not exist: there is
        nothing to report on, and a 404 says so more clearly than a report about nothing.
        """
        record = await self._require_backup(request.backup_id)
        target_type = GuestType(record.guest_type)
        target_vmid = self._resolve_target_vmid(request, record)
        target_storage = request.target_storage or (await self._agent_settings()).default_storage
        agent_node = (await self._proxmox_settings()).node
        target_node = request.target_node or agent_node

        checks: list[PreflightCheck] = []
        warnings: list[str] = []

        checks.append(_backup_restorable(record))

        artifact, artifact_error = await self._local_artifact(record)
        source, presence = self._artifact_presence(
            record, artifact=artifact, error=artifact_error, warnings=warnings
        )
        checks.append(presence)
        checks.append(
            self._checksum_check(record, artifact=artifact, source=source, warnings=warnings)
        )

        existing = await self._existing_guest(target_vmid)
        checks.append(self._target_free_check(target_vmid, existing, request, warnings=warnings))
        checks.append(_target_type_check(target_vmid, existing, target_type))
        checks.append(_target_stopped_check(target_vmid, existing, request))
        checks.append(_node_check(target_node, agent_node=agent_node))

        checks.append(
            await self._free_space_check(record, storage=target_storage, warnings=warnings)
        )
        checks.append(await self._in_flight_check())

        if record.node and record.node != target_node:
            warnings.append(
                f"This backup was taken on node '{record.node}' and will be restored on "
                f"'{target_node}'."
            )

        return PreflightReport(
            checks=checks,
            blocking=any(not check.ok for check in checks),
            warnings=warnings,
            source=source,
            backup_id=record.id,
            filename=record.filename,
            size_bytes=record.size_bytes,
            target_vmid=target_vmid,
            target_type=target_type,
            target_storage=target_storage,
            target_node=target_node,
        )

    # ---- two-phase flow ------------------------------------------------------

    async def create(
        self, request: RestoreRequest, *, requested_by: int | None
    ) -> tuple[RestoreHistory, str]:
        """Record the intent and mint a confirmation token.

        Returns the row and the **plaintext** token. Only the token's SHA-256 is stored: a
        database dump must not be enough to confirm a restore, and the value is shown exactly
        once — the report it belongs to is on the row.
        """
        report = await self.preflight(request)
        if report.blocking:
            raise ValidationFailed(
                "The restore was not created because a preflight check failed: "
                + ", ".join(report.failed),
                extra={"preflight": report.model_dump(mode="json")},
            )

        token = f"{CONFIRMATION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)

        record = await self._restores.create(
            backup_id=request.backup_id,
            source=report.source,
            target_vmid=report.target_vmid,
            target_type=report.target_type,
            restore_mode=request.restore_mode,
            target_node=report.target_node,
            target_storage=report.target_storage,
            overwrite_existing=request.overwrite_existing,
            force_stop=request.force_stop,
            start_after_restore=request.start_after_restore,
            confirmation_token_hash=_hash_token(token),
            confirmation_expires_at=expires_at,
            preflight=report.model_dump(mode="json"),
            requested_by=requested_by,
            correlation_id=get_correlation_id() or "",
        )

        logger.info(
            "restore_requested",
            restore_id=record.id,
            backup_id=request.backup_id,
            target_vmid=report.target_vmid,
            target_type=report.target_type.value,
            source=report.source.value,
            overwrite=request.overwrite_existing,
        )
        return record, token

    async def confirm(self, restore_id: int, payload: RestoreConfirmRequest) -> RestoreHistory:
        """Authorise a pending restore, after proving the request is still safe.

        Validation happens before any mutation, in the order that keeps a refusal cheap: state,
        expiry, token, target, then the live re-check that costs agent calls.
        """
        record = await self.get(restore_id)

        if record.status == RestoreStatus.EXPIRED.value:
            raise Conflict(
                f"Restore #{restore_id} expired before it was confirmed. Request it again."
            )
        if record.status != RestoreStatus.PENDING_CONFIRMATION.value:
            raise Conflict(
                f"Restore #{restore_id} is '{record.status}' and is no longer awaiting "
                "confirmation."
            )

        now = datetime.now(UTC)
        if record.confirmation_expires_at is not None and record.confirmation_expires_at <= now:
            # Refused here, closed by `expire_stale`. Writing the transition on the way out of
            # a rejected request would leave partially-applied state behind a 4xx, which the
            # session dependency's contract forbids; the executor's sweep owns that write and
            # runs in its own transaction. Until it does, the row keeps blocking retention —
            # the safe direction to be late in.
            raise Conflict(
                f"Restore #{restore_id}'s confirmation window has closed. Request it again."
            )

        if not _token_matches(payload.confirmation_token, record.confirmation_token_hash):
            raise ValidationFailed("The confirmation token does not match this restore.")
        if payload.target_vmid != record.target_vmid:
            raise ValidationFailed(
                f"This restore targets VMID {record.target_vmid}, "
                f"but {payload.target_vmid} was typed to confirm it."
            )

        report = await self.preflight(_request_from(record))
        if report.blocking:
            raise Conflict(
                "The restore was not started because conditions changed since it was "
                "requested: " + ", ".join(report.failed),
                extra={"preflight": report.model_dump(mode="json")},
            )

        record.status = RestoreStatus.CONFIRMED.value
        record.confirmed_at = now
        record.source = report.source.value
        record.preflight = report.model_dump(mode="json")
        # Single use. A token that stayed valid after confirmation would let a replayed
        # request re-authorise a restore that has already been cancelled.
        record.confirmation_token_hash = None
        await self._restores.flush()

        logger.info(
            "restore_confirmed",
            restore_id=restore_id,
            backup_id=record.backup_id,
            target_vmid=record.target_vmid,
            source=record.source,
        )
        return record

    async def cancel(self, restore_id: int) -> RestoreHistory:
        """Cancel a restore that has not reached the host.

        A `running` restore cannot be stopped from here: the executor owns the agent task id,
        and the row reaches its terminal state when the agent reports back.
        """
        record = await self.get(restore_id)
        status = RestoreStatus(record.status)

        if status is RestoreStatus.RUNNING:
            raise Conflict(
                f"Restore #{restore_id} is already running on the host. Cancelling it is "
                "handled by the executor."
            )
        if status.is_terminal:
            raise Conflict(f"Restore #{restore_id} is already '{record.status}'.")

        record.status = RestoreStatus.CANCELLED.value
        record.finished_at = datetime.now(UTC)
        record.confirmation_token_hash = None
        record.error_message = "Cancelled before it started"
        await self._restores.flush()

        logger.info("restore_cancelled", restore_id=restore_id, previous_status=status.value)
        return record

    async def expire_stale(self, *, now: datetime | None = None) -> list[int]:
        """Close abandoned confirmation windows. Returns the ids that changed."""
        expired = await self._restores.expire_pending(now=now or datetime.now(UTC))
        if expired:
            logger.info("restores_expired", count=len(expired))
        return expired

    # ---- individual checks ---------------------------------------------------

    async def _local_artifact(
        self, record: BackupHistory
    ) -> tuple[AgentArtifact | None, str | None]:
        """Ask the host what it actually holds.

        `local_deleted_at` on the history row is ProxSync's belief; this is the fact. They can
        differ in both directions — an artifact removed outside ProxSync, or one downloaded
        back from Drive by an earlier restore — and a restore must act on the fact.
        """
        if record.filename is None:
            return None, None
        try:
            artifacts = await self._agent.list_backups(
                vmid=record.vmid, guest_type=GuestType(record.guest_type)
            )
        except (AgentUnavailable, AgentError) as exc:
            return None, str(exc.detail)
        return next((item for item in artifacts if item.filename == record.filename), None), None

    def _artifact_presence(
        self,
        record: BackupHistory,
        *,
        artifact: AgentArtifact | None,
        error: str | None,
        warnings: list[str],
    ) -> tuple[RestoreSource, PreflightCheck]:
        name = "backup_present_locally"
        if error is not None:
            return RestoreSource.LOCAL, PreflightCheck(
                name=name,
                ok=False,
                detail=f"The host could not be asked whether the archive is present: {error}",
            )
        if artifact is not None:
            return RestoreSource.LOCAL, PreflightCheck(
                name=name, ok=True, detail=f"{artifact.filename} is on the host"
            )

        remote_usable = (
            record.upload_status == UploadStatus.UPLOADED.value
            and record.remote_path is not None
            and record.remote_deleted_at is None
        )
        if remote_usable:
            warnings.append(
                "The archive is not on the host and will be downloaded from Google Drive "
                "first, which can take considerably longer than the restore itself."
            )
            return RestoreSource.GDRIVE, PreflightCheck(
                name=name,
                ok=True,
                detail="Not on the host; it will be downloaded from Google Drive first",
            )

        return RestoreSource.LOCAL, PreflightCheck(
            name=name,
            ok=False,
            detail="The archive is on neither the host nor the remote, so there is nothing "
            "to restore from",
        )

    def _checksum_check(
        self,
        record: BackupHistory,
        *,
        artifact: AgentArtifact | None,
        source: RestoreSource,
        warnings: list[str],
    ) -> PreflightCheck:
        """Compare the recorded digest with what the host reports.

        A missing digest is a warning, not a refusal: a `vzdump` whose checksum could not be
        computed still produced a real backup, and refusing to restore it would leave an
        operator with a usable archive they are not allowed to use. What the digest buys is
        enforcement — when one is recorded it is handed to the agent, which verifies it before
        spawning anything.
        """
        name = "checksum_matches"
        expected = record.checksum_sha256

        if expected is None:
            warnings.append(
                "No checksum was recorded for this backup, so the archive cannot be verified "
                "before it is restored."
            )
            return PreflightCheck(
                name=name, ok=True, detail="No checksum was recorded for this backup"
            )

        if source is RestoreSource.GDRIVE:
            return PreflightCheck(
                name=name,
                ok=True,
                detail="The downloaded archive is verified against its recorded digest on the "
                "host before the restore starts",
            )

        actual = artifact.checksum_sha256 if artifact is not None else None
        if actual is None:
            return PreflightCheck(
                name=name,
                ok=True,
                detail="The host reports no digest yet; the archive is verified against the "
                "recorded value before the restore starts",
            )

        if not hmac.compare_digest(actual.lower(), expected.lower()):
            return PreflightCheck(
                name=name,
                ok=False,
                detail=f"The archive on the host does not match the recorded digest "
                f"(expected {expected[:16]}…, found {actual[:16]}…)",
            )
        return PreflightCheck(name=name, ok=True, detail=f"sha256 {expected[:16]}… matches")

    async def _existing_guest(self, vmid: int) -> Guest | None:
        """Look the VMID up across *both* guest types.

        Proxmox VMIDs are unique across VMs and containers, so restoring a VM onto a VMID held
        by a container is a collision — one that a type-scoped lookup would miss entirely.
        """
        for guest_type in GuestType:
            guest = await self._guests.get_by_vmid(vmid, guest_type)
            if guest is not None:
                return guest
        return None

    def _target_free_check(
        self,
        vmid: int,
        existing: Guest | None,
        request: RestoreRequest,
        *,
        warnings: list[str],
    ) -> PreflightCheck:
        name = "target_vmid_free"
        if existing is None:
            return PreflightCheck(name=name, ok=True, detail=f"VMID {vmid} is free")

        label = _guest_label(existing)
        if not request.overwrite_existing:
            return PreflightCheck(
                name=name,
                ok=False,
                detail=f"VMID {vmid} is in use by {label}. Set overwrite_existing to replace it.",
            )
        warnings.append(f"{label} will be destroyed and replaced by this restore.")
        return PreflightCheck(
            name=name, ok=True, detail=f"VMID {vmid} ({label}) will be overwritten"
        )

    async def _free_space_check(
        self, record: BackupHistory, *, storage: str, warnings: list[str]
    ) -> PreflightCheck:
        name = "storage_free_space"
        try:
            status: AgentStorageStatus = await self._agent.storage_status()
        except (AgentUnavailable, AgentError) as exc:
            return PreflightCheck(
                name=name,
                ok=False,
                detail=f"Free space on '{storage}' could not be measured: {exc.detail}",
            )

        entry = next((item for item in status.storages if item.name == storage), None)
        if entry is None:
            available = ", ".join(sorted(item.name for item in status.storages)) or "none"
            return PreflightCheck(
                name=name,
                ok=False,
                detail=f"The host does not report a storage called '{storage}'. "
                f"Available: {available}.",
            )
        if not entry.active:
            return PreflightCheck(
                name=name, ok=False, detail=f"Storage '{storage}' is not active on the host"
            )

        if not record.size_bytes:
            warnings.append(
                f"The size of this backup is not recorded, so free space on '{storage}' "
                "could not be checked against it."
            )
            return PreflightCheck(
                name=name,
                ok=True,
                detail=f"{_format_bytes(entry.available_bytes)} free; the backup's size is "
                "unknown, so no requirement could be computed",
            )

        required = math.ceil(record.size_bytes * FREE_SPACE_MARGIN)
        detail = (
            f"{_format_bytes(entry.available_bytes)} free, "
            f"{_format_bytes(required)} required on '{storage}'"
        )
        return PreflightCheck(name=name, ok=entry.available_bytes >= required, detail=detail)

    async def _in_flight_check(self) -> PreflightCheck:
        existing = await self._restores.in_flight()
        if existing is None:
            return PreflightCheck(
                name="no_restore_in_flight", ok=True, detail="No other restore is authorised"
            )
        return PreflightCheck(
            name="no_restore_in_flight",
            ok=False,
            detail=f"Restore #{existing.id} is '{existing.status}'. Restores are serialised, "
            "so it must finish or be cancelled first.",
        )

    # ---- helpers -------------------------------------------------------------

    async def _require_backup(self, backup_id: int) -> BackupHistory:
        record = await self._history.get(backup_id)
        if record is None:
            raise NotFound(f"No backup with id {backup_id}")
        return record

    @staticmethod
    def _resolve_target_vmid(request: RestoreRequest, record: BackupHistory) -> int:
        if request.restore_mode is RestoreMode.ORIGINAL_ID:
            return record.vmid
        assert request.target_vmid is not None  # guaranteed by the request model
        return request.target_vmid

    async def _agent_settings(self) -> AgentSettings:
        section = await self._settings_service.get_section(SettingsSection.AGENT)
        assert isinstance(section, AgentSettings)
        return section

    async def _proxmox_settings(self) -> ProxmoxSettings:
        section = await self._settings_service.get_section(SettingsSection.PROXMOX)
        assert isinstance(section, ProxmoxSettings)
        return section


# ---- pure check builders -----------------------------------------------------


def _backup_restorable(record: BackupHistory) -> PreflightCheck:
    name = "backup_restorable"
    if record.filename is None:
        return PreflightCheck(
            name=name, ok=False, detail=f"Backup #{record.id} never produced an artifact"
        )
    if record.status != BackupStatus.SUCCESS.value:
        return PreflightCheck(
            name=name,
            ok=False,
            detail=f"Backup #{record.id} is '{record.status}', so its artifact cannot be "
            "trusted as a restore source",
        )
    return PreflightCheck(
        name=name, ok=True, detail=f"{record.filename} (vmid {record.vmid}, {record.guest_type})"
    )


def _target_type_check(vmid: int, existing: Guest | None, target_type: GuestType) -> PreflightCheck:
    name = "target_type_matches"
    if existing is None:
        return PreflightCheck(name=name, ok=True, detail=f"No existing guest occupies VMID {vmid}")

    if existing.guest_type != target_type.value:
        return PreflightCheck(
            name=name,
            ok=False,
            detail=f"VMID {vmid} is a {existing.guest_type}; a {target_type.value} backup "
            "cannot replace it. Remove it on the host first, or restore to a new VMID.",
        )
    return PreflightCheck(
        name=name, ok=True, detail=f"VMID {vmid} is a {target_type.value}, as the backup is"
    )


def _target_stopped_check(
    vmid: int, existing: Guest | None, request: RestoreRequest
) -> PreflightCheck:
    """Whether the target may be replaced as things stand.

    The inventory can be minutes stale, so this is an early refusal rather than a guarantee —
    the agent checks the guest's live status again and refuses the same way.
    """
    name = "target_guest_stopped"
    if existing is None:
        return PreflightCheck(name=name, ok=True, detail=f"Nothing is running at VMID {vmid}")

    if existing.status != GuestStatus.RUNNING.value:
        return PreflightCheck(name=name, ok=True, detail=f"{_guest_label(existing)} is not running")
    if request.force_stop:
        return PreflightCheck(
            name=name,
            ok=True,
            detail=f"{_guest_label(existing)} is running and will be stopped first",
        )
    return PreflightCheck(
        name=name,
        ok=False,
        detail=f"{_guest_label(existing)} is running. Set force_stop to shut it down before "
        "restoring over it.",
    )


def _node_check(target_node: str, *, agent_node: str) -> PreflightCheck:
    """The agent restores on the node it runs on, and only there.

    Accepting a different `target_node` would mean silently restoring somewhere other than
    where the caller asked — which, for an operation that overwrites a guest, is the worst
    possible way to be wrong.
    """
    name = "target_node_supported"
    if target_node == agent_node:
        return PreflightCheck(name=name, ok=True, detail=f"Restoring on node '{target_node}'")
    return PreflightCheck(
        name=name,
        ok=False,
        detail=f"ProxSync's agent runs on node '{agent_node}'; restoring to '{target_node}' "
        "is not supported.",
    )


# ---- small helpers -----------------------------------------------------------


def _request_from(record: RestoreHistory) -> RestoreRequest:
    """Rebuild the original request from the stored row, for the re-check at confirmation."""
    return RestoreRequest(
        backup_id=record.backup_id,
        restore_mode=RestoreMode(record.restore_mode),
        target_vmid=record.target_vmid,
        target_storage=record.target_storage,
        target_node=record.target_node,
        overwrite_existing=record.overwrite_existing,
        force_stop=record.force_stop,
        start_after_restore=record.start_after_restore,
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_matches(token: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(_hash_token(token), stored_hash)


def _guest_label(guest: Guest) -> str:
    label = f"{guest.guest_type} {guest.vmid}"
    return f"{label} ({guest.name})" if guest.name else label


def _format_bytes(value: int) -> str:
    """Binary units, one decimal. Used only in preflight details an operator reads."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"  # pragma: no cover - the loop always returns
