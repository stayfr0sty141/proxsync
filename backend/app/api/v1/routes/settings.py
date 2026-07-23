"""Settings endpoints.

Reads require admin: several sections describe the backup topology, and one carries a
configured-secret indicator. Writes are a merge — send only what changed.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body

from app.api.deps import (
    AuditRepositoryDep,
    ContainerDep,
    RequireAdmin,
    SessionDep,
    SettingsServiceDep,
)
from app.schemas.enums import AuditAction, SettingsSection
from app.schemas.settings import AllSettingsResponse, SectionResponse

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", summary="All settings sections")
async def get_all(service: SettingsServiceDep, user: RequireAdmin) -> AllSettingsResponse:
    del user
    return await service.get_all()


@router.get("/{section}", summary="One settings section")
async def get_section(
    section: SettingsSection, service: SettingsServiceDep, user: RequireAdmin
) -> SectionResponse:
    del user
    return await service.get_section_response(section)


@router.put("/{section}", summary="Update a settings section")
async def update_section(
    section: SettingsSection,
    service: SettingsServiceDep,
    user: RequireAdmin,
    audit: AuditRepositoryDep,
    session: SessionDep,
    container: ContainerDep,
    payload: Annotated[dict[str, Any], Body()],
) -> SectionResponse:
    async with container.retention_guard.transaction(session):
        result = await service.update_section(section, payload, updated_by=user.id)
        await audit.record(
            action=AuditAction.SETTINGS_CHANGED,
            user_id=user.id,
            username=user.username,
            resource_type="settings_section",
            resource_id=section.value,
            # Names only. Values may include secrets, and an audit row is not a place for those.
            detail={"fields": sorted(payload)},
        )
        if section in {SettingsSection.RETENTION, SettingsSection.GDRIVE}:
            # The new policy becomes visible only when this transaction commits. A full rescan
            # then re-derives one canonical trigger per guest under the updated settings.
            container.retention_worker.notify_after_commit(session, None)
    return result
