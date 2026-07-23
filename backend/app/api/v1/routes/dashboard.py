"""Dashboard overview endpoints.

Two read-only projections backing the frontend's overview page (docs/API.md §
Dashboard): a summary of the fleet, backup cadence, storage and agent health, and
a merged recent-activity feed. Both require only the viewer role because they
expose nothing a viewer cannot already see on the individual pages — they only
compose it. Neither endpoint mutates state, so the session commits an empty
transaction, consistent with the other read routes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DashboardServiceDep, RequireViewer
from app.schemas.dashboard import DashboardActivityResponse, DashboardSummaryResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", summary="Fleet, backup, storage and agent overview")
async def dashboard_summary(
    service: DashboardServiceDep, user: RequireViewer
) -> DashboardSummaryResponse:
    del user
    return await service.summary()


@router.get("/activity", summary="Merged recent backup, restore and sync activity")
async def dashboard_activity(
    service: DashboardServiceDep,
    user: RequireViewer,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DashboardActivityResponse:
    del user
    return await service.activity(limit=limit)
