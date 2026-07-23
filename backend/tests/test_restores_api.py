"""Restore endpoints: the two-phase flow over HTTP, its roles, and its audit trail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.backup import BackupHistory, RestoreHistory
from app.db.models.system import AuditEvent
from app.repositories.restore_repository import SqlAlchemyRestoreRepository
from app.schemas.enums import (
    AuditAction,
    BackupStatus,
    GuestStatus,
    GuestType,
    RestoreMode,
    RestoreSource,
    RestoreStatus,
    UploadStatus,
)

from .conftest import ApiClient, StubAgentTransport, make_guest, seed_guests

ARCHIVE = "vzdump-qemu-101-2026_07_26-01_00_04.vma.zst"
CHECKSUM = "b" * 64
SIZE = 8 * 1024**3


def artifact_payload() -> list[dict[str, Any]]:
    return [
        {
            "filename": ARCHIVE,
            "path": f"/mnt/backup-hdd/dump/{ARCHIVE}",
            "vmid": 101,
            "guest_type": "vm",
            "size_bytes": SIZE,
            "created_at": "2026-07-26T01:00:04Z",
            "modified_at": "2026-07-26T01:12:00Z",
            "compression": "zstd",
            "checksum_sha256": CHECKSUM,
        }
    ]


def storage_payload(*, available: int = 500 * 1024**3) -> dict[str, Any]:
    return {
        "dump_root": {
            "path": "/mnt/backup-hdd/dump",
            "total_bytes": 1000,
            "used_bytes": 100,
            "free_bytes": 900,
            "used_percent": 10.0,
        },
        "storages": [
            {
                "name": "local-lvm",
                "type": "lvmthin",
                "active": True,
                "total_bytes": available * 2,
                "used_bytes": available,
                "available_bytes": available,
                "used_percent": 50.0,
            }
        ],
        "artifact_count": 1,
        "artifact_bytes": SIZE,
    }


def queue_preflight(agent: StubAgentTransport, *, available: int = 500 * 1024**3) -> None:
    """Preflight asks two questions, in this order, every time it runs."""
    agent.queue(200, artifact_payload())
    agent.queue(200, storage_payload(available=available))


async def seed_backup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: BackupStatus = BackupStatus.SUCCESS,
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
            filename=ARCHIVE,
            size_bytes=SIZE,
            checksum_sha256=CHECKSUM,
            status=status.value,
            upload_status=UploadStatus.NOT_REQUIRED.value,
            started_at=datetime.now(UTC),
        )
        session.add(record)
        await session.commit()
        return record.id


def request_body(backup_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "backup_id": backup_id,
        "restore_mode": "new_id",
        "target_vmid": 151,
        "target_storage": "local-lvm",
        **overrides,
    }


class TestPreflightEndpoint:
    async def test_reports_without_creating_anything(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        queue_preflight(agent_transport)

        response = authenticated_client.post("/api/v1/restores/preflight", request_body(backup_id))

        assert response.status_code == 200
        body = response.json()
        assert body["blocking"] is False
        assert {check["name"] for check in body["checks"]} >= {
            "backup_present_locally",
            "checksum_matches",
            "target_vmid_free",
            "storage_free_space",
            "no_restore_in_flight",
        }

        listing = authenticated_client.get("/api/v1/restores")
        assert listing.json()["total"] == 0

    async def test_a_blocking_report_is_a_200_with_the_reason(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        """ "This would not work, here is why" is an answer, not an error."""
        backup_id = await seed_backup(session_factory)
        await seed_guests(session_factory, make_guest(151, name="busy", status=GuestStatus.RUNNING))
        queue_preflight(agent_transport)

        response = authenticated_client.post("/api/v1/restores/preflight", request_body(backup_id))

        assert response.status_code == 200
        body = response.json()
        assert body["blocking"] is True
        failed = {check["name"] for check in body["checks"] if not check["ok"]}
        assert "target_vmid_free" in failed

    async def test_a_viewer_may_not_preflight_a_restore(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)
        authenticated_client.logout_local()

        response = authenticated_client.post(
            "/api/v1/restores/preflight", request_body(backup_id), csrf=False
        )
        assert response.status_code == 401


class TestCreateAndConfirm:
    async def test_the_full_two_phase_flow(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        queue_preflight(agent_transport)

        created = authenticated_client.post("/api/v1/restores", request_body(backup_id))
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "pending_confirmation"
        assert body["confirmation_token"].startswith("rst_")
        assert body["expires_at"] is not None
        restore_id = body["id"]

        queue_preflight(agent_transport)  # confirmation re-runs every check
        confirmed = authenticated_client.post(
            f"/api/v1/restores/{restore_id}/confirm",
            {"confirmation_token": body["confirmation_token"], "target_vmid": 151},
        )

        assert confirmed.status_code == 202
        assert confirmed.json()["status"] == "confirmed"

        async with session_factory() as session:
            actions = list((await session.execute(select(AuditEvent.action))).scalars())
        assert AuditAction.RESTORE_REQUESTED.value in actions
        assert AuditAction.RESTORE_CONFIRMED.value in actions

    async def test_the_token_is_never_returned_again(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        queue_preflight(agent_transport)
        restore_id = authenticated_client.post("/api/v1/restores", request_body(backup_id)).json()[
            "id"
        ]

        detail = authenticated_client.get(f"/api/v1/restores/{restore_id}")

        assert detail.status_code == 200
        assert "confirmation_token" not in detail.json()

    async def test_confirming_with_the_wrong_vmid_is_refused(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        queue_preflight(agent_transport)
        created = authenticated_client.post("/api/v1/restores", request_body(backup_id)).json()

        response = authenticated_client.post(
            f"/api/v1/restores/{created['id']}/confirm",
            {"confirmation_token": created["confirmation_token"], "target_vmid": 152},
        )

        assert response.status_code == 400
        async with session_factory() as session:
            stored = await SqlAlchemyRestoreRepository(session).get(created["id"])
            assert stored is not None
            assert stored.status == RestoreStatus.PENDING_CONFIRMATION.value

    async def test_an_expired_request_never_executes(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        queue_preflight(agent_transport)
        created = authenticated_client.post("/api/v1/restores", request_body(backup_id)).json()

        async with session_factory() as session:
            record = await SqlAlchemyRestoreRepository(session).get(created["id"])
            assert record is not None
            record.confirmation_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        response = authenticated_client.post(
            f"/api/v1/restores/{created['id']}/confirm",
            {"confirmation_token": created["confirmation_token"], "target_vmid": 151},
        )

        assert response.status_code == 409
        # Still pending: the refusal writes nothing, and the executor's sweep is what closes
        # the window. Late in the safe direction — the row keeps blocking retention meanwhile.
        assert authenticated_client.get(f"/api/v1/restores/{created['id']}").json()["status"] == (
            RestoreStatus.PENDING_CONFIRMATION.value
        )

    async def test_a_blocking_report_refuses_to_create(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory, status=BackupStatus.FAILED)
        queue_preflight(agent_transport)

        response = authenticated_client.post("/api/v1/restores", request_body(backup_id))

        assert response.status_code == 400
        assert response.json()["preflight"]["blocking"] is True

    async def test_new_id_without_a_target_vmid_is_a_schema_error(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)

        response = authenticated_client.post(
            "/api/v1/restores",
            {"backup_id": backup_id, "restore_mode": "new_id", "target_storage": "local-lvm"},
        )

        assert response.status_code == 422


class TestCancelAndReads:
    async def test_cancelling_a_pending_restore(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        queue_preflight(agent_transport)
        created = authenticated_client.post("/api/v1/restores", request_body(backup_id)).json()

        response = authenticated_client.post(f"/api/v1/restores/{created['id']}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == RestoreStatus.CANCELLED.value
        async with session_factory() as session:
            actions = list((await session.execute(select(AuditEvent.action))).scalars())
        assert AuditAction.RESTORE_CANCELLED.value in actions

    async def test_a_running_restore_cannot_be_cancelled_without_the_executor(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Workers are off in the suite, so nothing owns the agent task id — and the endpoint
        says so rather than marking a restore cancelled that is still rewriting a guest."""
        backup_id = await seed_backup(session_factory)
        restore_id = await seed_restore(session_factory, backup_id, RestoreStatus.RUNNING)

        response = authenticated_client.post(f"/api/v1/restores/{restore_id}/cancel")

        assert response.status_code == 409

    async def test_listing_filters_by_status_and_backup(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)
        await seed_restore(session_factory, backup_id, RestoreStatus.SUCCESS)
        await seed_restore(session_factory, backup_id, RestoreStatus.FAILED)

        everything = authenticated_client.get("/api/v1/restores")
        succeeded = authenticated_client.get("/api/v1/restores?status=success")
        other = authenticated_client.get("/api/v1/restores?backup_id=9999")

        assert everything.json()["total"] == 2
        assert succeeded.json()["total"] == 1
        assert succeeded.json()["items"][0]["filename"] == ARCHIVE
        assert other.json()["total"] == 0

    async def test_a_restore_that_never_reached_the_host_has_no_log(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)
        restore_id = await seed_restore(session_factory, backup_id, RestoreStatus.FAILED)

        response = authenticated_client.get(f"/api/v1/restores/{restore_id}/log")

        assert response.status_code == 404

    async def test_the_log_is_read_from_the_agent(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        agent_transport: StubAgentTransport,
    ) -> None:
        backup_id = await seed_backup(session_factory)
        restore_id = await seed_restore(
            session_factory, backup_id, RestoreStatus.SUCCESS, agent_task_id="task-9"
        )
        agent_transport.queue(
            200,
            {
                "task_id": "task-9",
                "lines": ["restore vma archive", "Total bytes read: 9126805504"],
                "truncated": False,
                "total_lines": 2,
            },
        )

        response = authenticated_client.get(f"/api/v1/restores/{restore_id}/log")

        assert response.status_code == 200
        assert response.json()["total_lines"] == 2

    async def test_deleting_a_backup_a_restore_needs_is_refused(
        self,
        authenticated_client: ApiClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        backup_id = await seed_backup(session_factory)
        await seed_restore(session_factory, backup_id, RestoreStatus.CONFIRMED)

        response = authenticated_client.delete(f"/api/v1/backups/{backup_id}")

        assert response.status_code == 409
        assert "needs this artifact" in response.json()["detail"]


async def seed_restore(
    session_factory: async_sessionmaker[AsyncSession],
    backup_id: int,
    status: RestoreStatus,
    *,
    agent_task_id: str | None = None,
) -> int:
    async with session_factory() as session:
        record = RestoreHistory(
            backup_id=backup_id,
            source=RestoreSource.LOCAL.value,
            target_vmid=151,
            target_type=GuestType.VM.value,
            restore_mode=RestoreMode.NEW_ID.value,
            target_node="pve",
            target_storage="local-lvm",
            status=status.value,
            agent_task_id=agent_task_id,
            correlation_id="seeded",
        )
        session.add(record)
        await session.commit()
        return record.id
