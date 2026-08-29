"""`/admin/enroll-with-cp` is observable when the CP is unreachable.

Before this fix, only a non-success HTTP *status* from the control plane
emitted `cp.enroll_failed` and raised 502. A transport failure — connection
refused, DNS failure, timeout — out of either `client.post` call was
uncaught, so it surfaced as a bare FastAPI 500 with no telemetry at all: the
exact reason `PENDING.md` P11's flake (a transport failure at this same call)
was undiagnosable.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import aitp

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import agent_admin  # noqa: E402
from revocation_state import RevocationState  # noqa: E402


def _app(manifest_json: str) -> FastAPI:
    agent = aitp.AitpAgent.generate()
    bootstrap = {"cp": {"base_url": "http://cp.invalid", "aid": "aid:pubkey:doesnotmatter"}}
    router = agent_admin.build_admin_router(
        agent=agent,
        bootstrap=bootstrap,
        held_tcts={},
        revocation=RevocationState(),
        manifest_provider=lambda: manifest_json,
    )
    app = FastAPI()
    app.include_router(router)
    return app


def _stub_agent_admin_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Rebind `agent_admin`'s own `httpx` name — same technique as
    `test_revocation_verify_or_discard.py`'s `_stub_transport`, and for the
    same reason: this must not touch the shared `httpx` module object other
    importers (including `revocation_refresh`) see.
    """
    def factory(*args, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # agent_admin.py also references `httpx.HTTPError` (the exception type
    # its own transport-failure guard catches), so the stand-in needs it too
    # — not just AsyncClient.
    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )


def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def _capture(event_type, _bootstrap, **fields):
        captured.append({"type": event_type, **fields})

    monkeypatch.setattr(agent_admin, "emit_event", _capture)
    return captured


def test_a_transport_failure_at_enroll_returns_502_with_cp_enroll_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _stub_agent_admin_transport(monkeypatch, handler)
    events = _events(monkeypatch)

    client = TestClient(_app('{"manifest": {}}'))
    resp = client.post("/admin/enroll-with-cp", json={})

    assert resp.status_code == 502
    failures = [e for e in events if e["type"] == "cp.enroll_failed"]
    assert len(failures) == 1
    assert failures[0]["stage"] == "enroll"
    assert "transport" in failures[0]


def test_a_transport_failure_at_register_returns_502_with_cp_enroll_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/registry/enroll":
            return httpx.Response(200, json={"token": "tok", "expiresIn": 300})
        raise httpx.ConnectError("connection refused")

    _stub_agent_admin_transport(monkeypatch, handler)
    events = _events(monkeypatch)

    client = TestClient(_app('{"manifest": {}}'))
    resp = client.post("/admin/enroll-with-cp", json={})

    assert resp.status_code == 502
    failures = [e for e in events if e["type"] == "cp.enroll_failed"]
    assert len(failures) == 1
    assert failures[0]["stage"] == "register"
    assert "transport" in failures[0]


def test_a_non_json_2xx_enroll_response_returns_502_with_cp_enroll_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    _stub_agent_admin_transport(monkeypatch, handler)
    events = _events(monkeypatch)

    client = TestClient(_app('{"manifest": {}}'))
    resp = client.post("/admin/enroll-with-cp", json={})

    assert resp.status_code == 502
    failures = [e for e in events if e["type"] == "cp.enroll_failed"]
    assert len(failures) == 1
    assert failures[0]["stage"] == "enroll"
