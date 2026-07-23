"""API v1 router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    auth,
    backup_jobs,
    backups,
    dashboard,
    events,
    guests,
    health,
    logs,
    notifications,
    restores,
    retention,
    runs,
    settings,
    storage,
    sync,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(settings.router)
api_router.include_router(guests.router)
api_router.include_router(backup_jobs.router)
api_router.include_router(backups.router)
api_router.include_router(runs.router)
api_router.include_router(sync.router)
api_router.include_router(restores.router)
api_router.include_router(retention.router)
api_router.include_router(storage.router)
api_router.include_router(notifications.router)
api_router.include_router(logs.router)
api_router.include_router(events.router)
