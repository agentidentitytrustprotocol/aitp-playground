"""Issue #95: an agent's /admin routes require the supervisor's admin token.

Covers the agent-side guard (agents/base/admin_auth.py), the service-side
httpx hook that attaches the token (hosting/supervisor.py) and the bootstrap
that carries it between the two.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest
from admin_auth import AdminAuthMiddleware
from fastapi import FastAPI

from aitp_playground.config import Settings
from aitp_playground.hosting.bootstrap import BootstrapBuilder
from aitp_playground.hosting.supervisor import (
    ADMIN_TOKEN_HEADER,
    AdminTokenAuth,
    AgentSupervisor,
    RunningAgent,
)
from aitp_playground.registry.models import AgentSpec
from aitp_playground.registry.service import RegistryService

TOKEN = "s3cret-token"


def _agent_app() -> FastAPI:
    app = FastAPI()

    @app.post("/admin/trust-peers")
    async def trust_peers() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/admin/process-renewal")
    async def process_renewal() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/.well-known/aitp-manifest")
    async def manifest() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/administrator")
    async def lookalike() -> dict[str, bool]:
        return {"ok": True}

    return app


def _client(token: str | None) -> httpx.AsyncClient:
    guarded = AdminAuthMiddleware(_agent_app(), token=token)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://agent")


@pytest.mark.parametrize("headers", [{}, {ADMIN_TOKEN_HEADER: "wrong"}, {ADMIN_TOKEN_HEADER: ""}])
async def test_admin_route_refuses_missing_or_wrong_token(headers: dict[str, str]) -> None:
    async with _client(TOKEN) as c:
        r = await c.post("/admin/trust-peers", headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": "admin token missing or invalid"}


async def test_admin_route_accepts_the_token() -> None:
    async with _client(TOKEN) as c:
        r = await c.post("/admin/trust-peers", headers={ADMIN_TOKEN_HEADER: TOKEN})
    assert r.status_code == 200


async def test_no_token_configured_refuses_every_admin_call() -> None:
    """A bootstrap without a token must not mean an open /admin."""
    async with _client(None) as c:
        r = await c.post("/admin/trust-peers", headers={ADMIN_TOKEN_HEADER: TOKEN})
    assert r.status_code == 401


async def test_protocol_and_lookalike_paths_stay_open() -> None:
    """Only /admin and /admin/* are guarded; /admin/process-renewal is
    authenticated by the signed renewal request instead (holder → issuer)."""
    async with _client(TOKEN) as c:
        assert (await c.get("/.well-known/aitp-manifest")).status_code == 200
        assert (await c.get("/administrator")).status_code == 200
        assert (await c.post("/admin/process-renewal")).status_code == 200


def test_run_agent_requires_admin_token_keyword() -> None:
    import inspect

    from aitp_server import run_agent

    param = inspect.signature(run_agent).parameters["admin_token"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_every_agent_entrypoint_passes_the_bootstrap_token() -> None:
    root = Path(__file__).resolve().parents[2] / "agents"
    for main in sorted(root.glob("*/main.py")):
        assert 'admin_token=bootstrap.get("admin_token")' in main.read_text(), main


def _send(auth: httpx.Auth, url: str) -> httpx.Request:
    flow = auth.auth_flow(httpx.Request("POST", url))
    return next(flow)


def test_auth_hook_attaches_token_only_to_loopback_admin_of_a_known_port() -> None:
    auth = AdminTokenAuth({9100: TOKEN})
    assert _send(auth, "http://localhost:9100/admin/trust-peers").headers[ADMIN_TOKEN_HEADER] == TOKEN
    assert _send(auth, "http://127.0.0.1:9100/admin/invoke").headers[ADMIN_TOKEN_HEADER] == TOKEN
    for url in (
        "http://localhost:9101/admin/trust-peers",       # not a launched agent
        "http://localhost:9100/.well-known/aitp-manifest",  # not /admin
        "https://org-b.aitp.test:9100/admin/invoke",     # never off-box
        "http://localhost:9100/administrator",
    ):
        assert ADMIN_TOKEN_HEADER not in _send(auth, url).headers, url


def test_supervisor_forgets_token_on_kill() -> None:
    sup = AgentSupervisor()
    sup._agents["r:a"] = RunningAgent(
        run_id="r", agent_id="a", port=9100, pid=None, aid="", manifest_url="", status="ready",
    )
    sup._admin_tokens[9100] = TOKEN
    sup.kill("r", "a")
    assert 9100 not in sup._admin_tokens
    assert ADMIN_TOKEN_HEADER not in _send(sup.admin_auth, "http://localhost:9100/admin/x").headers


def test_token_is_not_serialized_with_running_agent() -> None:
    """/agents returns asdict(RunningAgent); the token must not be on it."""
    assert "admin_token" not in RunningAgent.__dataclass_fields__


def test_bootstrap_carries_fresh_token_and_is_written_0600() -> None:
    settings = Settings()
    manifest = RegistryService(settings).get_agent_manifest("_shared/agents/researcher")
    builder = BootstrapBuilder(settings)
    kwargs = dict(
        run_id="run-admin-token",
        agent_spec=AgentSpec(id="researcher", ref="_shared/agents/researcher"),
        resolved_manifest=manifest, port=8100, peers={}, inputs={},
    )
    a, b = builder.build(**kwargs), builder.build(**kwargs)
    assert len(a["admin_token"]) >= 40
    assert a["admin_token"] != b["admin_token"]

    path = Path(builder.write(a))
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["admin_token"] == a["admin_token"]
    finally:
        path.unlink()
