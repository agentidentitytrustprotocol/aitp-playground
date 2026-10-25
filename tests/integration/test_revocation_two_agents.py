"""PG1.1 acceptance across two in-process agents (#103, criteria 1–3).

A initiates a handshake with B, so B issues A a TCT and B is the agent that
verifies it whenever A presents it. Revoking that TCT "in the console" is a
`GET /api/revocations?iss=<B>` answer that now names its jti; B picks it up on
its own poll, signs it into its own list and rejects A's next call.

1. Before the revocation, B accepts A's TCT.
2. After it, B rejects the TCT within one refresh interval, with no admin call
   in between: the poll loop is what carries it.
3. With the CP stopped, B keeps serving the same signed list (discoverable
   through B's Manifest and verifiable against B's AID) and keeps rejecting,
   until the list's signed `expires_at`. Past that there is nothing to serve
   and B's posture is degraded, so under `fail_closed` it refuses everything
   rather than falling back to an empty list.

Both agents are the real `AitpServer` + admin router pair the worker mains
wire up, mounted as ASGI apps. Only the network is replaced: A's admin client
dispatches into B's app, and B's CP client into a fake CP.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi import FastAPI, Request

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import agent_admin  # noqa: E402
import aitp_server  # noqa: E402
import bootstrap as agent_bootstrap  # noqa: E402
import revocation_publish  # noqa: E402
from agent_admin import build_admin_router  # noqa: E402
from aitp_server import AitpServer  # noqa: E402
from revocation_state import RevocationState  # noqa: E402

B_ORIGIN = "http://b:9"
GRANT = "demo.write"
POLL_SECS = 0.25
LIST_TTL_SECS = 60


class FakeCp:
    """CP1.1's `GET /api/revocations`: entries per issuer, and an off switch."""

    def __init__(self) -> None:
        self.entries: dict[str, list[dict[str, Any]]] = {}
        self.down = False

    def revoke(self, iss: str, jti: str) -> None:
        self.entries.setdefault(iss, []).append(
            {"jti": jti, "revoked_at": int(time.time()), "reason": "console"}
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("control plane stopped")
        iss = request.url.params["iss"]
        return httpx.Response(200, json={"iss": iss, "entries": self.entries.get(iss, [])})


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, module: Any, transport: Callable[[], Any]) -> None:
    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport())

    monkeypatch.setattr(
        module, "httpx", types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )


@pytest.fixture
def cp(monkeypatch: pytest.MonkeyPatch) -> FakeCp:
    fake = FakeCp()
    _patch_httpx(monkeypatch, revocation_publish, lambda: httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(aitp_server, "emit_event", emit)
    monkeypatch.setattr(agent_admin, "emit_event", emit)


def _agent_b() -> tuple[AitpServer, FastAPI]:
    """The issuer and verifier: a CP-backed worker serving one capability."""
    bootstrap = {
        "run_id": "r", "agent_id": "b",
        "aitp": {
            "seed_hex": uuid.uuid4().hex * 2, "display_name": "b",
            "handshake_endpoint": f"{B_ORIGIN}/aitp/handshake/hello",
            "offered_caps": [GRANT],
        },
        "cp": {
            "base_url": "http://cp", "api_key": "admin-key",
            "fail_mode": "fail_closed", "max_staleness_secs": 300,
            "list_ttl_secs": LIST_TTL_SECS,
        },
    }
    agent = agent_bootstrap.create_agent(bootstrap)
    revocation = RevocationState()
    server = AitpServer(
        agent=agent, manifest_json=agent_bootstrap.get_manifest_json(agent, bootstrap),
        port=9, bootstrap=bootstrap, revocation=revocation,
    )
    # A sub-second cadence so "within one refresh interval" is measurable.
    server.revocation_poll_secs = POLL_SECS  # type: ignore[assignment]
    app = FastAPI()
    app.include_router(server.router)
    app.include_router(build_admin_router(
        agent=agent, bootstrap=bootstrap, held_tcts={}, revocation=revocation,
        manifest_provider=server._fresh_manifest_json, issued_tcts=server._issued_tcts,
    ))

    @app.post(f"/capabilities/{GRANT}")
    async def capability(request: Request) -> dict[str, Any]:
        identity = server.verify_capability_tct(
            request.headers.get("x-aitp-tct", ""), GRANT,
            pop_response=request.headers.get("x-aitp-pop-response", ""),
        )
        return {"ok": True, "grants": list(identity.grants)}

    return server, app


def _agent_a() -> tuple["aitp.AitpAgent", FastAPI]:
    """The holder: an initiator with only the admin router."""
    agent = aitp.AitpAgent.generate()
    agent.build_manifest(
        display_name="a", handshake_endpoint="http://a/aitp/handshake/hello",
        offered_caps=["demo.read"], revocation_list_uri=f"http://a{aitp.REVOCATION_LIST_PATH}",
    )
    bootstrap = {"run_id": "r", "agent_id": "a", "aitp": {}}
    app = FastAPI()
    app.include_router(build_admin_router(
        agent=agent, bootstrap=bootstrap, held_tcts={}, revocation=RevocationState(),
    ))
    return agent, app


async def _call_b(a: httpx.AsyncClient) -> dict[str, Any]:
    r = await a.post("/admin/invoke", json={
        "peer_port": 9, "capability": GRANT, "payload": {"x": 1}, "peer_base_url": B_ORIGIN,
    })
    assert r.status_code == 200, r.text
    return r.json()


async def _b_list_via_manifest(b: httpx.AsyncClient, b_aid: str) -> str:
    """What a peer does: find B's list through B's Manifest, verify it as B's."""
    manifest = (await b.get("/.well-known/aitp-manifest")).json()["manifest"]
    uri = manifest["extensions"]["rfc-aitp-0008.revocation_list_uri"]
    assert uri == f"{B_ORIGIN}{aitp.REVOCATION_LIST_PATH}"
    resp = await b.get(uri)
    assert resp.status_code == 200, resp.text
    aitp.verify_revocation_list(resp.text, b_aid)
    return resp.text


async def test_a_console_revocation_reaches_b_within_one_poll_and_survives_a_cp_outage(
    monkeypatch: pytest.MonkeyPatch, cp: FakeCp,
) -> None:
    server_b, app_b = _agent_b()
    a_agent, app_a = _agent_a()
    server_b.trusted_peer_aids.add(a_agent.aid)
    _patch_httpx(monkeypatch, agent_admin, lambda: httpx.ASGITransport(app=app_b))

    # What ready_lifespan does before AITP_AGENT_READY.
    assert (await server_b.refresh_revocations_now(quiet=False))["verified"] is True

    a = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a")
    b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url=B_ORIGIN)
    poll = server_b.start_revocation_poll()
    assert poll is not None
    try:
        hs = await a.post("/admin/initiate-handshake", json={
            "peer_manifest_url": f"{B_ORIGIN}/.well-known/aitp-manifest",
            "requested_grants": [GRANT],
        })
        assert hs.status_code == 200, hs.text
        jti = hs.json()["jti"]
        assert hs.json()["peer_aid"] == server_b.agent.aid

        # ── 2. before the revocation, B accepts ────────────────────────
        accepted = await _call_b(a)
        assert accepted == {"ok": True, "grants": [GRANT]}

        # ── 1. revoke in the console; B rejects within one interval ────
        cp.revoke(server_b.agent.aid, jti)
        revoked_at = time.monotonic()
        while True:
            outcome = await _call_b(a)
            if outcome.get("error"):
                break
            assert time.monotonic() - revoked_at < POLL_SECS * 4, (
                "B still accepts a revoked TCT after several poll intervals"
            )
            await asyncio.sleep(POLL_SECS / 10)
        assert time.monotonic() - revoked_at <= POLL_SECS * 2
        assert outcome["status_code"] == 403
        assert outcome["body"] == {"detail": f"tct revoked (issuer-list): jti={jti}"}

        listed = await _b_list_via_manifest(b, server_b.agent.aid)
        assert [e["jti"] for e in json.loads(listed)["revocation_list"]["entries"]] == [jti]

        # ── 3. stop the CP: the list stays served, B keeps rejecting ───
        cp.down = True
        await asyncio.sleep(POLL_SECS * 3)  # several failed pulls
        assert await _b_list_via_manifest(b, server_b.agent.aid) == listed
        assert (await _call_b(a))["status_code"] == 403

        # ... until the signed expires_at, and not past it.
        past_expiry = time.time() + LIST_TTL_SECS + 1
        assert server_b.issued_revocations.serveable(now=past_expiry) is None
        assert server_b.revocation.posture(
            can_verify=server_b.can_verify_revocation,
            max_staleness_secs=10**6, now=past_expiry,
        ) == "degraded"
    finally:
        await server_b.stop_revocation_poll()
        await a.aclose()
        await b.aclose()
