"""HTTP client for the Backup Agent.

The only component in the dashboard permitted to reach the privileged host service. It signs
every request exactly the way `agent/app/core/security.py` verifies it — canonical string
``METHOD|path?query|timestamp|nonce|sha256(body)`` — and the body it signs is the byte string
it sends, not a re-serialisation, because those can differ.

Retries apply to **GET only**. A POST that starts a backup is not idempotent: retrying a
request whose response was lost would start a second vzdump.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import secrets
import time
from typing import Any, Self

import httpx

from app.clients.circuit_breaker import CircuitBreaker, CircuitState
from app.core.config import Settings
from app.core.errors import AgentError, AgentUnavailable, ConfigurationError
from app.core.logging import get_correlation_id, logger
from app.schemas.agent import (
    AgentArtifact,
    AgentDeletedArtifact,
    AgentHealth,
    AgentRemoteList,
    AgentRemoteQuota,
    AgentStorageStatus,
    AgentTask,
    AgentTaskAccepted,
    AgentTaskLog,
    AgentVerifyResult,
)
from app.schemas.enums import BackupMode, Compression, GuestType

HEADER_KEY_ID = "X-ProxSync-Key"
HEADER_TIMESTAMP = "X-ProxSync-Timestamp"
HEADER_NONCE = "X-ProxSync-Nonce"
HEADER_SIGNATURE = "X-ProxSync-Signature"
HEADER_CORRELATION_ID = "X-Correlation-ID"

_RETRYABLE_METHODS = frozenset({"GET"})
_BACKOFF_BASE_SECONDS = 0.25
_BACKOFF_MAX_SECONDS = 4.0


def build_canonical_string(
    *, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> str:
    return f"{method.upper()}|{path}|{timestamp}|{nonce}|{hashlib.sha256(body).hexdigest()}"


def sign(*, secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    canonical = build_canonical_string(
        method=method, path=path, timestamp=timestamp, nonce=nonce, body=body
    )
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


class AgentClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._secret = settings.agent_hmac_secret.get_secret_value()
        self._client = client or self._build_client(settings)
        self._breaker = CircuitBreaker(
            failure_threshold=settings.agent_circuit_failure_threshold,
            reset_seconds=settings.agent_circuit_reset_seconds,
        )

    @staticmethod
    def _build_client(settings: Settings) -> httpx.AsyncClient:
        verify: bool | str = settings.agent_verify_tls
        if settings.agent_ca_cert is not None:
            verify = str(settings.agent_ca_cert)

        cert: Any = None
        if settings.agent_client_cert and settings.agent_client_key:
            cert = (str(settings.agent_client_cert), str(settings.agent_client_key))

        return httpx.AsyncClient(
            base_url=settings.agent_base_url.rstrip("/"),
            verify=verify,
            cert=cert,
            timeout=httpx.Timeout(
                settings.agent_timeout_seconds, connect=settings.agent_connect_timeout_seconds
            ),
            headers={"User-Agent": "proxsync-dashboard"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def circuit_state(self) -> CircuitState:
        return self._breaker.state()

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    # ---- transport -----------------------------------------------------------

    def _headers(self, method: str, signed_path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        headers = {
            HEADER_KEY_ID: self._settings.agent_api_key_id,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(
                secret=self._secret,
                method=method,
                path=signed_path,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
            ),
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            headers[HEADER_CORRELATION_ID] = correlation_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if not self._secret:
            raise ConfigurationError(
                "PROXSYNC_AGENT_HMAC_SECRET is not set; the dashboard cannot reach the "
                "Backup Agent. Copy the value printed by install-agent.sh."
            )

        if not self._breaker.allow():
            raise AgentUnavailable(
                "The Backup Agent is unreachable; requests are paused for "
                f"{self._breaker.retry_after_seconds()}s. "
                "Check that proxsync-agent is running on the Proxmox host."
            )

        # The body must be signed exactly as sent, so serialise once and pass bytes through.
        body = b"" if payload is None else json.dumps(payload).encode()
        query = httpx.QueryParams(params or {})
        signed_path = f"{path}?{query}" if str(query) else path

        headers = self._headers(method, signed_path, body)
        if payload is not None:
            headers["Content-Type"] = "application/json"

        attempts = self._settings.agent_max_retries + 1 if method in _RETRYABLE_METHODS else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, path, params=params, content=body, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "agent_request_transport_error",
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(_backoff(attempt))
                    # A retry is a new request: re-sign so the timestamp stays inside the window.
                    headers = self._headers(method, signed_path, body)
                    if payload is not None:
                        headers["Content-Type"] = "application/json"
                    continue
                break
            else:
                self._breaker.record_success()
                return self._parse(response, method=method, path=path)

        self._breaker.record_failure()
        raise AgentUnavailable(
            f"Could not reach the Backup Agent at {self._settings.agent_base_url}: {last_error}"
        )

    def _parse(self, response: httpx.Response, *, method: str, path: str) -> Any:
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

        problem: dict[str, Any] | None = None
        detail = response.text[:500]
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                problem = parsed
                detail = str(parsed.get("detail") or parsed.get("title") or detail)
        except ValueError:
            pass

        logger.warning(
            "agent_request_rejected",
            method=method,
            path=path,
            status=response.status_code,
            detail=detail,
        )

        if response.status_code in {401, 403}:
            raise AgentError(
                "The Backup Agent rejected the dashboard's credentials. Check that the HMAC "
                "secret matches and that both clocks are synchronised.",
                agent_status=response.status_code,
                problem=problem,
            )
        raise AgentError(detail, agent_status=response.status_code, problem=problem)

    # ---- operations ----------------------------------------------------------

    async def health(self) -> AgentHealth:
        return AgentHealth.model_validate(await self._request("GET", "/health"))

    async def start_backup(
        self,
        *,
        vmid: int,
        guest_type: GuestType,
        mode: BackupMode,
        compression: Compression,
        storage: str,
        zstd_threads: int | None = None,
        bwlimit_kbps: int = 0,
        notes: str | None = None,
    ) -> AgentTaskAccepted:
        payload: dict[str, Any] = {
            "vmid": vmid,
            "guest_type": guest_type.value,
            "mode": mode.value,
            "compression": compression.value,
            "storage": storage,
            "bwlimit_kbps": bwlimit_kbps,
        }
        if zstd_threads is not None:
            payload["zstd_threads"] = zstd_threads
        if notes:
            payload["notes"] = notes
        correlation_id = get_correlation_id()
        if correlation_id:
            payload["correlation_id"] = correlation_id

        return AgentTaskAccepted.model_validate(
            await self._request("POST", "/backup/start", payload=payload)
        )

    async def list_backups(
        self, *, vmid: int | None = None, guest_type: GuestType | None = None
    ) -> list[AgentArtifact]:
        params: dict[str, Any] = {}
        if vmid is not None:
            params["vmid"] = vmid
        if guest_type is not None:
            params["guest_type"] = guest_type.value
        raw = await self._request("GET", "/backup/list", params=params or None)
        return [AgentArtifact.model_validate(item) for item in raw]

    async def delete_backup(self, filename: str) -> AgentDeletedArtifact:
        return AgentDeletedArtifact.model_validate(
            await self._request("DELETE", f"/backup/{filename}")
        )

    async def start_restore(
        self,
        *,
        guest_type: GuestType,
        archive: str,
        target_vmid: int,
        storage: str,
        overwrite: bool = False,
        force_stop: bool = False,
        start_after: bool = False,
        bwlimit_kbps: int = 0,
        expected_sha256: str | None = None,
        unprivileged: bool | None = None,
    ) -> AgentTaskAccepted:
        """Start a restore on the host.

        The endpoint is chosen from the *artifact's* guest type, never from anything the
        caller can name independently: the agent refuses a VM archive on the LXC route, and
        picking the route here from the same fact keeps that refusal a redundancy rather than
        the only line of defence.
        """
        payload: dict[str, Any] = {
            "archive": archive,
            "target_vmid": target_vmid,
            "storage": storage,
            "overwrite": overwrite,
            "force_stop": force_stop,
            "start_after": start_after,
            "bwlimit_kbps": bwlimit_kbps,
        }
        if expected_sha256:
            payload["expected_sha256"] = expected_sha256

        path = "/restore/vm"
        if guest_type is GuestType.LXC:
            path = "/restore/lxc"
            if unprivileged is not None:
                payload["unprivileged"] = unprivileged

        return AgentTaskAccepted.model_validate(
            await self._request("POST", path, payload=self._with_correlation(payload))
        )

    async def get_task(self, task_id: str) -> AgentTask:
        return AgentTask.model_validate(await self._request("GET", f"/task/{task_id}"))

    async def get_task_log(self, task_id: str, *, tail: int = 500) -> AgentTaskLog:
        return AgentTaskLog.model_validate(
            await self._request("GET", f"/task/{task_id}/log", params={"tail": tail})
        )

    async def cancel_task(self, task_id: str) -> AgentTask:
        return AgentTask.model_validate(await self._request("POST", f"/task/{task_id}/cancel"))

    async def storage_status(self) -> AgentStorageStatus:
        return AgentStorageStatus.model_validate(await self._request("GET", "/storage/status"))

    # ---- Google Drive sync ---------------------------------------------------

    async def start_upload(
        self,
        *,
        filename: str,
        remote: str,
        remote_path: str,
        bwlimit_kbps: int = 0,
        transfers: int | None = None,
        verify_after: bool = False,
    ) -> AgentTaskAccepted:
        payload: dict[str, Any] = {
            "filename": filename,
            "remote": remote,
            "remote_path": remote_path,
            "bwlimit_kbps": bwlimit_kbps,
            "verify_after": verify_after,
        }
        if transfers is not None:
            payload["transfers"] = transfers
        return AgentTaskAccepted.model_validate(
            await self._request("POST", "/sync/upload", payload=self._with_correlation(payload))
        )

    async def start_download(
        self,
        *,
        filename: str,
        remote: str,
        remote_path: str,
        bwlimit_kbps: int = 0,
        overwrite: bool = False,
    ) -> AgentTaskAccepted:
        payload: dict[str, Any] = {
            "filename": filename,
            "remote": remote,
            "remote_path": remote_path,
            "bwlimit_kbps": bwlimit_kbps,
            "overwrite": overwrite,
        }
        return AgentTaskAccepted.model_validate(
            await self._request("POST", "/sync/download", payload=self._with_correlation(payload))
        )

    async def verify_remote(
        self, *, filename: str, remote: str, remote_path: str
    ) -> AgentVerifyResult:
        payload: dict[str, Any] = {
            "filename": filename,
            "remote": remote,
            "remote_path": remote_path,
        }
        return AgentVerifyResult.model_validate(
            await self._request("POST", "/sync/verify", payload=self._with_correlation(payload))
        )

    async def list_remote(self, *, remote: str, remote_path: str = "") -> AgentRemoteList:
        return AgentRemoteList.model_validate(
            await self._request(
                "GET", "/sync/list", params={"remote": remote, "remote_path": remote_path}
            )
        )

    async def remote_quota(self, *, remote: str) -> AgentRemoteQuota:
        return AgentRemoteQuota.model_validate(
            await self._request("GET", "/sync/about", params={"remote": remote})
        )

    async def delete_remote(
        self, *, filename: str, remote: str, remote_path: str
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "filename": filename,
            "remote": remote,
            "remote_path": remote_path,
        }
        result = await self._request(
            "POST", "/sync/delete", payload=self._with_correlation(payload)
        )
        return dict(result or {})

    @staticmethod
    def _with_correlation(payload: dict[str, Any]) -> dict[str, Any]:
        correlation_id = get_correlation_id()
        if correlation_id and "correlation_id" not in payload:
            return {**payload, "correlation_id": correlation_id}
        return payload


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter, so parallel callers do not retry in lockstep."""
    ceiling = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2**attempt))
    return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography
