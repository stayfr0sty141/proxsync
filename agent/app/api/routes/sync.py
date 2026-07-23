"""Google Drive sync endpoints (decision D1).

These are extension endpoints, not part of the six the brief names. They exist because rclone
must run where the artifacts are, and the alternative — a copy through the dashboard LXC —
would double the network cost of every upload for no benefit.

The vocabulary stays closed: a remote must pass the agent's allow-list, a filename must be a
well-formed vzdump artifact, and a remote path may not contain traversal or rclone's filter
metacharacters.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ContainerDep
from app.schemas.requests import (
    SyncDeleteRequest,
    SyncDownloadRequest,
    SyncUploadRequest,
    SyncVerifyRequest,
)
from app.schemas.responses import (
    RemoteEntryResponse,
    RemoteListResponse,
    RemoteQuotaResponse,
    TaskAcceptedResponse,
    VerifyResultResponse,
)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post(
    "/upload", status_code=status.HTTP_202_ACCEPTED, summary="Upload an artifact to a remote"
)
async def upload(request: SyncUploadRequest, container: ContainerDep) -> TaskAcceptedResponse:
    task = container.sync_service.upload(request)
    return TaskAcceptedResponse(
        task_id=task.id,
        state=task.state,
        kind=task.kind,
        created_at=task.created_at,
        correlation_id=task.correlation_id,
    )


@router.post(
    "/download",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Download an artifact from a remote",
)
async def download(request: SyncDownloadRequest, container: ContainerDep) -> TaskAcceptedResponse:
    task = container.sync_service.download(request)
    return TaskAcceptedResponse(
        task_id=task.id,
        state=task.state,
        kind=task.kind,
        created_at=task.created_at,
        correlation_id=task.correlation_id,
    )


@router.post("/verify", summary="Compare a local artifact with its remote copy")
async def verify(request: SyncVerifyRequest, container: ContainerDep) -> VerifyResultResponse:
    """Synchronous: the remote listing is one API call, and the local digest is cached in a
    sidecar after the first run."""
    return await container.sync_service.verify(request)


@router.get("/list", summary="List a remote directory")
async def list_remote(
    container: ContainerDep,
    remote: Annotated[str, Query(min_length=1, max_length=64)],
    remote_path: Annotated[str, Query(max_length=1024)] = "",
) -> RemoteListResponse:
    entries = await container.sync_service.list_remote(remote=remote, remote_path=remote_path)
    return RemoteListResponse(
        remote=remote,
        remote_path=remote_path,
        entries=[
            RemoteEntryResponse(
                name=entry.name,
                path=entry.path,
                size_bytes=entry.size_bytes,
                is_dir=entry.is_dir,
                modified_at=entry.modified_at,
                md5=entry.md5,
                hashes=entry.hashes,
            )
            for entry in entries
        ],
    )


@router.get("/about", summary="Remote quota")
async def about(
    container: ContainerDep,
    remote: Annotated[str, Query(min_length=1, max_length=64)],
) -> RemoteQuotaResponse:
    quota = await container.sync_service.quota(remote=remote)
    return RemoteQuotaResponse(
        remote=remote,
        total_bytes=quota.total_bytes,
        used_bytes=quota.used_bytes,
        free_bytes=quota.free_bytes,
        trashed_bytes=quota.trashed_bytes,
        used_percent=quota.used_percent,
    )


@router.post("/delete", summary="Delete one file from a remote")
async def delete_remote(request: SyncDeleteRequest, container: ContainerDep) -> dict[str, str]:
    """POST rather than DELETE: the remote name, path and filename are three separate
    validated fields, and a URL that concatenated them would invite exactly the traversal
    this endpoint refuses."""
    target = await container.sync_service.delete_remote(request)
    return {"deleted": target}
