"""Read-only Proxmox VE API client (decision D2).

The dashboard needs to know which guests exist. It does *not* need — and must not have — the
ability to change them. That is enforced twice:

1. The API token is documented as `PVEAuditor`, which cannot mutate anything.
2. This class exposes exactly one verb, `_get`. There is no `_post`, so no code path in the
   dashboard can issue a write to the host even if the token were over-privileged by mistake.

Everything that mutates a guest goes through the Backup Agent instead, where it is validated
against a closed command vocabulary.
"""

from __future__ import annotations

from typing import Any, Self

import httpx

from app.core.config import Settings
from app.core.errors import ConfigurationError, UpstreamError
from app.core.logging import logger
from app.schemas.enums import GuestType
from app.schemas.proxmox import DiscoveredGuest, PveGuest, PveNode, PveVersion

API_PREFIX = "/api2/json"

_ENDPOINT_BY_TYPE = {GuestType.VM: "qemu", GuestType.LXC: "lxc"}


class ProxmoxClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> httpx.AsyncClient:
        verify: bool | str = settings.proxmox_verify_tls
        if settings.proxmox_ca_cert is not None:
            verify = str(settings.proxmox_ca_cert)

        return httpx.AsyncClient(
            base_url=settings.proxmox_base_url.rstrip("/"),
            verify=verify,
            timeout=httpx.Timeout(settings.proxmox_timeout_seconds, connect=5.0),
            headers={"User-Agent": "proxsync-dashboard"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def configured(self) -> bool:
        return self._settings.proxmox_configured

    @property
    def node(self) -> str:
        return self._settings.proxmox_node

    # ---- transport -----------------------------------------------------------

    def _auth_header(self) -> dict[str, str]:
        token_id = self._settings.proxmox_token_id
        secret = self._settings.proxmox_token_secret.get_secret_value()
        return {"Authorization": f"PVEAPIToken={token_id}={secret}"}

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            raise ConfigurationError(
                "PROXSYNC_PROXMOX_TOKEN_ID and PROXSYNC_PROXMOX_TOKEN_SECRET are not set; "
                "the dashboard cannot read the guest inventory. Create a PVEAuditor token: "
                "pveum user token add proxsync@pve dashboard --privsep 0"
            )

        url = f"{API_PREFIX}{path}"
        try:
            response = await self._client.get(url, params=params, headers=self._auth_header())
        except httpx.HTTPError as exc:
            logger.warning("proxmox_request_failed", path=url, error=str(exc))
            raise UpstreamError(
                f"Could not reach the Proxmox API at {self._settings.proxmox_base_url}: {exc}"
            ) from exc

        return self._parse(response, path=url)

    @staticmethod
    def _parse(response: httpx.Response, *, path: str) -> Any:
        if response.status_code in {401, 403}:
            logger.warning("proxmox_request_denied", path=path, status=response.status_code)
            raise ConfigurationError(
                "Proxmox rejected the API token. Check PROXSYNC_PROXMOX_TOKEN_ID / _SECRET, "
                "and that the token holds the PVEAuditor role on '/'."
            )
        if not response.is_success:
            detail = response.text[:300]
            logger.warning("proxmox_request_error", path=path, status=response.status_code)
            raise UpstreamError(f"Proxmox returned {response.status_code} for {path}: {detail}")

        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError(f"Proxmox returned a non-JSON response for {path}") from exc

        if not isinstance(body, dict) or "data" not in body:
            raise UpstreamError(f"Unexpected Proxmox response shape for {path}")
        return body["data"]

    # ---- operations ----------------------------------------------------------

    async def version(self) -> PveVersion:
        return PveVersion.model_validate(await self._get("/version"))

    async def nodes(self) -> list[PveNode]:
        raw = await self._get("/nodes")
        return [PveNode.model_validate(item) for item in raw]

    async def guests_of_type(
        self, guest_type: GuestType, *, node: str | None = None
    ) -> list[PveGuest]:
        target = node or self.node
        raw = await self._get(f"/nodes/{target}/{_ENDPOINT_BY_TYPE[guest_type]}")
        return [PveGuest.model_validate(item) for item in raw]

    async def discover(self, *, node: str | None = None) -> list[DiscoveredGuest]:
        """Every VM and container on the node, in one flat list."""
        target = node or self.node
        discovered: list[DiscoveredGuest] = []
        for guest_type in GuestType:
            for guest in await self.guests_of_type(guest_type, node=target):
                discovered.append(DiscoveredGuest(guest_type=guest_type, node=target, guest=guest))
        return discovered
