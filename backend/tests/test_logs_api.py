"""`/logs`, `/logs/export` and `/audit`."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.audit_repository import SqlAlchemyAuditRepository
from app.repositories.log_repository import NewLogEntry, SqlAlchemyLogRepository
from app.schemas.enums import AuditAction, AuditResult, LogCategory, LogLevel

from .conftest import ApiClient

NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def entry(
    message: str,
    *,
    level: LogLevel = LogLevel.INFO,
    category: LogCategory = LogCategory.BACKUP,
    minutes_ago: int = 0,
    correlation_id: str | None = None,
    context: dict[str, object] | None = None,
) -> NewLogEntry:
    return NewLogEntry(
        ts=NOW - timedelta(minutes=minutes_ago),
        level=level,
        category=category,
        message=message,
        context=context,
        correlation_id=correlation_id,
    )


async def seed_logs(
    session_factory: async_sessionmaker[AsyncSession], *entries: NewLogEntry
) -> None:
    async with session_factory() as session:
        await SqlAlchemyLogRepository(session).add_many(entries)
        await session.commit()


async def default_logs(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_logs(
        session_factory,
        entry("vzdump exited 1", level=LogLevel.ERROR, minutes_ago=5, correlation_id="abc"),
        entry("backup_finished", minutes_ago=3),
        entry(
            "notification_sent",
            category=LogCategory.NOTIFY,
            minutes_ago=1,
            correlation_id="abc",
        ),
    )


# ---- search ------------------------------------------------------------------


async def test_logs_are_returned_newest_first(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)

    body = authenticated_client.get("/api/v1/logs").json()

    assert body["total"] == 3
    assert [item["message"] for item in body["items"]] == [
        "notification_sent",
        "backup_finished",
        "vzdump exited 1",
    ]


async def test_logs_filter_by_category_and_level(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)

    notify = authenticated_client.get("/api/v1/logs?category=notify").json()
    errors = authenticated_client.get("/api/v1/logs?level=error").json()

    assert [item["message"] for item in notify["items"]] == ["notification_sent"]
    assert [item["message"] for item in errors["items"]] == ["vzdump exited 1"]


async def test_a_correlation_id_pivots_to_the_whole_incident(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The fastest path from "the Sunday job failed" to the exact vzdump stderr line."""
    await default_logs(session_factory)

    body = authenticated_client.get("/api/v1/logs?correlation_id=abc").json()

    assert body["total"] == 2
    assert {item["message"] for item in body["items"]} == {
        "vzdump exited 1",
        "notification_sent",
    }


async def test_free_text_search_matches_the_message(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)

    body = authenticated_client.get("/api/v1/logs?search=vzdump").json()

    assert [item["message"] for item in body["items"]] == ["vzdump exited 1"]


async def test_a_time_range_bounds_the_result(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)
    since = (NOW - timedelta(minutes=4)).isoformat()

    # `params=`, not an f-string: the `+00:00` offset has to be percent-encoded, or the `+`
    # arrives as a space and the datetime fails to parse.
    body = authenticated_client.get("/api/v1/logs", params={"from": since}).json()

    assert body["total"] == 2


async def test_paging_reports_the_full_total(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)

    body = authenticated_client.get("/api/v1/logs?limit=1&offset=1").json()

    assert body["total"] == 3
    assert [item["message"] for item in body["items"]] == ["backup_finished"]


async def test_the_response_says_whether_logs_are_being_persisted_at_all(
    authenticated_client: ApiClient,
) -> None:
    """Workers are off in the suite, so nothing is draining the sink. An empty list has to read
    as "not recorded" rather than "nothing happened"."""
    body = authenticated_client.get("/api/v1/logs").json()

    assert body["persisted"] is True
    assert body["dropped"] == 0


async def test_logs_require_authentication(client: ApiClient) -> None:
    assert client.get("/api/v1/logs").status_code == 401


# ---- export ------------------------------------------------------------------


async def test_ndjson_export_streams_one_object_per_line(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)

    response = authenticated_client.get("/api/v1/logs/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in response.headers["content-disposition"]
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert len(lines) == 3
    assert {line["message"] for line in lines} == {
        "vzdump exited 1",
        "backup_finished",
        "notification_sent",
    }


async def test_csv_export_quotes_a_message_containing_a_comma(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Hand-rolled CSV corrupts every row after the first comma, and agent errors are full of
    them."""
    await seed_logs(
        session_factory,
        entry('vzdump failed: qmp command "freeze", exit 1', context={"vmid": 101}),
    )

    response = authenticated_client.get("/api/v1/logs/export?format=csv")

    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][0] == "id"
    assert rows[1][4] == 'vzdump failed: qmp command "freeze", exit 1'
    assert json.loads(rows[1][10]) == {"vmid": 101}


async def test_an_empty_csv_export_still_has_its_header(
    authenticated_client: ApiClient,
) -> None:
    response = authenticated_client.get("/api/v1/logs/export?format=csv")

    assert response.text.startswith("id,ts,level,category,message")


async def test_the_export_honours_the_same_filters(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await default_logs(session_factory)

    response = authenticated_client.get("/api/v1/logs/export?level=error")

    assert len(response.text.splitlines()) == 1
    assert "vzdump exited 1" in response.text


async def test_the_export_is_admin_only(client: ApiClient) -> None:
    assert client.get("/api/v1/logs/export").status_code == 401


# ---- audit -------------------------------------------------------------------


async def test_the_audit_trail_lists_and_filters(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        audit = SqlAlchemyAuditRepository(session)
        await audit.record(action=AuditAction.LOGIN_FAILURE, result=AuditResult.FAILURE)
        await audit.record(
            action=AuditAction.RESTORE_CONFIRMED,
            username="admin",
            resource_type="restore",
            resource_id="4",
        )
        await session.commit()

    everything = authenticated_client.get("/api/v1/audit").json()
    restores = authenticated_client.get("/api/v1/audit?action=restore_confirmed").json()
    failures = authenticated_client.get("/api/v1/audit?result=failure").json()

    # The suite's own logins are in the trail too, so this asserts on presence, not on a count.
    assert everything["total"] >= 2
    assert restores["total"] == 1
    assert restores["items"][0]["resource_id"] == "4"
    assert all(item["result"] == "failure" for item in failures["items"])


async def test_the_audit_trail_searches_who_and_what(
    authenticated_client: ApiClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        await SqlAlchemyAuditRepository(session).record(
            action=AuditAction.BACKUP_DELETED,
            username="operator-jane",
            resource_type="backup",
            resource_id="812",
        )
        await session.commit()

    body = authenticated_client.get("/api/v1/audit?search=operator-jane").json()

    assert body["total"] == 1
    assert body["items"][0]["username"] == "operator-jane"


async def test_the_audit_trail_is_admin_only(client: ApiClient) -> None:
    assert client.get("/api/v1/audit").status_code == 401
