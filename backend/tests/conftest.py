"""Shared fixtures.

Tests run against a real SQLite file (not `:memory:`, which is per-connection and would give
each pool member its own empty database) and a real FastAPI app with its lifespan executed,
so bootstrap seeding, cookies and the dependency graph are all exercised as deployed.

Argon2 parameters are lowered to keep the suite fast. Every other security parameter is left
at its production value — a test that passes under weakened rules proves nothing.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.proxmox_client import ProxmoxClient
from app.core.config import Settings
from app.core.log_sink import log_sink
from app.db.models import Base
from app.db.models.guest import Guest
from app.db.session import Database
from app.main import create_app
from app.schemas.enums import GuestStatus, GuestType

SECRET_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef"
AGENT_SECRET = "agent-shared-secret-for-tests"  # noqa: S105
ADMIN_PASSWORD = "bootstrap-password-1"  # noqa: S105
PROXMOX_TOKEN_ID = "proxsync@pve!tests"
PROXMOX_TOKEN_SECRET = "11111111-2222-3333-4444-555555555555"  # noqa: S105


@pytest.fixture(autouse=True)
def isolated_log_sink() -> Iterator[None]:
    """Return the process-wide log sink to a clean state after every test.

    `create_app` arms it, and it outlives the app instance that did. Without this, one test
    that boots with workers enabled would leave capture on for every test after it — filling a
    buffer nobody drains, and charging its drop count to whichever test asserts on it next.
    """
    yield
    log_sink.reset()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="development",
        secret_key=SecretStr(SECRET_KEY),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        bootstrap_admin_username="admin",
        bootstrap_admin_password=SecretStr(ADMIN_PASSWORD),
        cookie_secure=False,  # TestClient speaks http://
        agent_hmac_secret=SecretStr(AGENT_SECRET),
        agent_base_url="https://agent.invalid:8765",
        proxmox_token_id=PROXMOX_TOKEN_ID,
        proxmox_token_secret=SecretStr(PROXMOX_TOKEN_SECRET),
        proxmox_base_url="https://pve.invalid:8006",
        proxmox_node="pve",
        # Off by default: a worker racing an assertion is the classic source of a flaky
        # suite. The tests that need one drive it directly, or opt in via `worker_settings`.
        background_workers=False,
        log_level="WARNING",
        log_json=False,
        # Argon2 at production cost would add ~50 ms to every login in the suite.
        argon2_time_cost=1,
        argon2_memory_cost=8,
        argon2_parallelism=1,
    )


@pytest.fixture
async def prepared_database(settings: Settings) -> AsyncIterator[None]:
    """Create the schema before the app starts.

    `create_all` rather than Alembic: it is faster, and `test_migrations.py` separately proves
    the migration and the models agree.
    """
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield


@pytest.fixture
async def session_factory(
    settings: Settings, prepared_database: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    del prepared_database
    # Database() rather than a bare engine: it installs the SQLite pragmas, so tests see the
    # same foreign-key enforcement and journal mode as production.
    database = Database(settings)
    yield database.session_factory
    await database.dispose()


@pytest.fixture
def client(
    settings: Settings,
    prepared_database: None,
    agent_transport: StubAgentTransport,
    proxmox_transport: StubProxmoxTransport,
) -> Iterator[ApiClient]:
    del prepared_database
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        install_stub_clients(app, agent=agent_transport, proxmox=proxmox_transport)
        yield ApiClient(test_client)


@pytest.fixture
def agent_transport() -> StubAgentTransport:
    return StubAgentTransport()


class ApiClient:
    """Thin wrapper that carries the access token and the CSRF header for us."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.access_token: str | None = None
        self.csrf_token: str | None = None

    @property
    def raw(self) -> TestClient:
        return self._client

    @property
    def cookies(self) -> Any:
        return self._client.cookies

    def _headers(self, *, csrf: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if csrf:
            # Read it from the jar, as a browser client would: the cookie rotates on refresh.
            current = self._client.cookies.get("proxsync_csrf") or self.csrf_token
            if current:
                headers["X-CSRF-Token"] = current
        if extra:
            headers.update(extra)
        return headers

    def login(self, username: str = "admin", password: str = ADMIN_PASSWORD) -> Any:
        response = self._client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        if response.status_code == 200:
            body = response.json()
            self.access_token = body["access_token"]
            self.csrf_token = body["csrf_token"]
        return response

    def logout_local(self) -> None:
        self.access_token = None
        self.csrf_token = None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._client.get(path, headers=self._headers(csrf=False), **kwargs)

    def post(self, path: str, json_body: Any = None, *, csrf: bool = True, **kwargs: Any) -> Any:
        return self._client.post(path, json=json_body, headers=self._headers(csrf=csrf), **kwargs)

    def put(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self._client.put(path, json=json_body, headers=self._headers(csrf=True), **kwargs)

    def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self._client.patch(path, json=json_body, headers=self._headers(csrf=True), **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._client.delete(path, headers=self._headers(csrf=True), **kwargs)


@pytest.fixture
def authenticated_client(client: ApiClient) -> ApiClient:
    """Signed in, and past the forced first-password change."""
    client.login()
    client.post(
        "/api/v1/auth/change-password",
        {"current_password": ADMIN_PASSWORD, "new_password": "a-much-better-password-2"},
    )
    client.login(password="a-much-better-password-2")
    return client


# ---- agent stubbing ----------------------------------------------------------


class RecordedRequest:
    def __init__(self, request: httpx.Request) -> None:
        self.method = request.method
        self.url = request.url
        self.headers = dict(request.headers)
        self.content = request.content

    @property
    def json_body(self) -> Any:
        return json.loads(self.content) if self.content else None


class StubAgentTransport(httpx.AsyncBaseTransport):
    """Records requests and replays canned responses, so the signing path is exercised
    end-to-end without a Proxmox host."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.responses: list[httpx.Response] = []
        self.raise_on_request: Exception | None = None

    def queue(self, status_code: int, payload: Any) -> None:
        self.responses.append(httpx.Response(status_code, json=payload))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(RecordedRequest(request))
        if self.raise_on_request is not None:
            raise self.raise_on_request
        if not self.responses:
            return httpx.Response(200, json={})
        return self.responses.pop(0)


class StubProxmoxTransport(httpx.AsyncBaseTransport):
    """Answers PVE inventory calls from a dict keyed by path suffix."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.routes: dict[str, tuple[int, Any]] = {}

    def route(self, suffix: str, payload: Any, *, status_code: int = 200) -> None:
        self.routes[suffix] = (status_code, payload)

    def set_guests(self, *, vms: list[Any] | None = None, lxcs: list[Any] | None = None) -> None:
        self.route("/qemu", {"data": vms or []})
        self.route("/lxc", {"data": lxcs or []})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(RecordedRequest(request))
        path = request.url.path
        for suffix, (status_code, payload) in self.routes.items():
            if path.endswith(suffix):
                return httpx.Response(status_code, json=payload)
        return httpx.Response(404, json={"data": None})


def pve_guest(vmid: int, name: str, *, status: str = "running", **extra: Any) -> dict[str, Any]:
    """A PVE guest payload in the shape the real API returns it, quirks included."""
    return {"vmid": vmid, "name": name, "status": status, "template": 0, **extra}


@pytest.fixture
def proxmox_transport() -> StubProxmoxTransport:
    return StubProxmoxTransport()


@pytest.fixture
def proxmox(settings: Settings, proxmox_transport: StubProxmoxTransport) -> ProxmoxClient:
    return ProxmoxClient(
        settings,
        client=httpx.AsyncClient(base_url=settings.proxmox_base_url, transport=proxmox_transport),
    )


def install_stub_clients(
    app: Any, *, agent: httpx.AsyncBaseTransport, proxmox: httpx.AsyncBaseTransport
) -> None:
    """Point the running app's clients at stub transports.

    The clients themselves are untouched — signing, retry and error mapping all still run —
    so a route test exercises the same code path as production, minus the network.
    """
    container = app.state.container
    container.agent._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        base_url=container.settings.agent_base_url, transport=agent
    )
    container.proxmox._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        base_url=container.settings.proxmox_base_url, transport=proxmox
    )


def agent_health_payload(status: str = "ok") -> dict[str, Any]:
    return {
        "status": status,
        "version": "0.1.0",
        "started_at": "2026-07-22T00:00:00Z",
        "uptime_seconds": 120.0,
        "hostname": "pve",
        "dump_root": "/mnt/backup-hdd/dump",
        "dump_root_writable": True,
        "binaries": {"vzdump": True, "qmrestore": True, "qm": True, "pct": True, "pvesm": True},
        "slots": [{"name": "backup", "in_use": 0, "capacity": 1}],
        "active_tasks": 0,
        "mtls_enabled": True,
    }


def verify_agent_signature(recorded: RecordedRequest, *, secret: str = AGENT_SECRET) -> None:
    """Re-implements the agent's verification, so a signing regression fails here."""
    import hashlib
    import hmac

    path = recorded.url.raw_path.decode()
    timestamp = recorded.headers["x-proxsync-timestamp"]
    nonce = recorded.headers["x-proxsync-nonce"]
    canonical = (
        f"{recorded.method.upper()}|{path}|{timestamp}|{nonce}|"
        f"{hashlib.sha256(recorded.content).hexdigest()}"
    )
    expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, recorded.headers["x-proxsync-signature"]), (
        "agent signature does not verify"
    )
    assert abs(time.time() - float(timestamp)) < 60, "timestamp outside the agent's window"
    assert len(nonce) >= 8


@pytest.fixture
def nonce() -> str:
    return uuid.uuid4().hex


# ---- inventory fixtures ------------------------------------------------------


def make_guest(
    vmid: int,
    *,
    guest_type: GuestType = GuestType.VM,
    name: str | None = None,
    node: str = "pve",
    enabled: bool = True,
    status: GuestStatus = GuestStatus.RUNNING,
) -> Guest:
    now = datetime.now(UTC)
    return Guest(
        vmid=vmid,
        guest_type=guest_type.value,
        name=name or f"guest-{vmid}",
        node=node,
        status=status.value,
        backup_enabled=enabled,
        tags=[],
        first_seen_at=now,
        last_seen_at=now,
    )


async def seed_guests(
    session_factory: async_sessionmaker[AsyncSession], *guests: Guest
) -> list[int]:
    async with session_factory() as session:
        for guest in guests:
            session.add(guest)
        await session.commit()
        return [guest.id for guest in guests]


@pytest.fixture
async def seeded_guests(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[Guest]:
    """Two VMs and one container, all enabled, plus one VM that is *not* enabled."""
    guests = [
        make_guest(101, name="web"),
        make_guest(102, name="db"),
        make_guest(201, guest_type=GuestType.LXC, name="proxy"),
        make_guest(103, name="scratch", enabled=False),
    ]
    await seed_guests(session_factory, *guests)
    return guests
