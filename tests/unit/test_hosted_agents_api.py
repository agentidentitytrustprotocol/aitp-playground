"""Route-level tests for the ``/hosted-agents`` HTTP surface (api/hosted.py)
not already covered by tests/unit/test_federation.py — which owns the
did:web http/https gate, the bootstrap public-origin plumbing, and the
fail-closed loopback/origin-mismatch guards on
``/hosted-agents/{id}/resolve-and-handshake``.

This file covers the rest of the route surface: ``POST /hosted-agents``
(host), ``GET /hosted-agents``, ``GET /hosted-agents/{id}``,
``DELETE /hosted-agents/{id}``, and ``POST /hosted-agents/{id}/invoke`` —
each success path plus an error path, against a fake ``HostedAgentManager``
so no subprocess is ever spawned. Follows the same "swap the module's httpx
reference for a MockTransport-backed client" pattern
tests/unit/test_engine_run.py uses for the runner engine.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aitp_playground.api import hosted as hosted_api_mod
from aitp_playground.api._deps import get_hosted_manager
from aitp_playground.api.hosted import router as hosted_router
from aitp_playground.hosting.hosted import HostedAgent


def _hosted_agent(hosted_id: str = "h1", port: int = 9100) -> HostedAgent:
    return HostedAgent(
        hosted_id=hosted_id,
        agent_id="analyzer",
        ref="_shared/agents/analyzer",
        port=port,
        aid="aid-analyzer",
        did="did:web:org-b.aitp.test",
        origin="https://org-b.aitp.test",
        manifest_url="https://org-b.aitp.test/.well-known/aitp-manifest",
        handshake_url="https://org-b.aitp.test/aitp/handshake/hello",
        did_document_url="https://org-b.aitp.test/.well-known/did.json",
    )


class FakeHostedManager:
    def __init__(self, *, host_error: Optional[Exception] = None) -> None:
        self.host_error = host_error
        self.host_calls: list[dict[str, Any]] = []
        self._hosted: dict[str, HostedAgent] = {}

    async def host(self, **kwargs: Any) -> HostedAgent:
        self.host_calls.append(kwargs)
        if self.host_error is not None:
            raise self.host_error
        h = _hosted_agent(hosted_id=f"h{len(self._hosted) + 1}")
        self._hosted[h.hosted_id] = h
        return h

    def get(self, hosted_id: str) -> Optional[HostedAgent]:
        return self._hosted.get(hosted_id)

    def list(self) -> list[dict[str, Any]]:
        return [asdict(h) for h in self._hosted.values()]

    def stop(self, hosted_id: str) -> bool:
        return self._hosted.pop(hosted_id, None) is not None

    def seed(self, hosted: HostedAgent) -> None:
        self._hosted[hosted.hosted_id] = hosted


def _app(mgr: FakeHostedManager) -> FastAPI:
    app = FastAPI()
    app.include_router(hosted_router)
    app.dependency_overrides[get_hosted_manager] = lambda: mgr
    return app


# --------------------------------------------------------------------------- #
# POST /hosted-agents
# --------------------------------------------------------------------------- #


def test_host_agent_returns_the_hosted_payload() -> None:
    mgr = FakeHostedManager()
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents", json={"ref": "_shared/agents/analyzer"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "analyzer"
    assert body["origin"] == "https://org-b.aitp.test"
    assert mgr.host_calls == [{
        "ref": "_shared/agents/analyzer", "public_host": None, "public_scheme": None,
        "signing_suite": None, "inputs": None, "port": None,
    }]


def test_host_agent_value_error_maps_to_400() -> None:
    mgr = FakeHostedManager(host_error=ValueError("no public host configured"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents", json={"ref": "_shared/agents/analyzer"})
    assert resp.status_code == 400, resp.text
    assert "no public host configured" in resp.text


def test_host_agent_unexpected_error_maps_to_502() -> None:
    mgr = FakeHostedManager(host_error=RuntimeError("adapter blew up"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents", json={"ref": "_shared/agents/analyzer"})
    assert resp.status_code == 502, resp.text
    assert "failed to host agent" in resp.text
    assert "adapter blew up" in resp.text


# --------------------------------------------------------------------------- #
# GET /hosted-agents, GET/DELETE /hosted-agents/{id}
# --------------------------------------------------------------------------- #


def test_list_agents_returns_every_hosted_agent() -> None:
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    mgr.seed(_hosted_agent("h2", port=9101))
    client = TestClient(_app(mgr))
    resp = client.get("/hosted-agents")
    assert resp.status_code == 200, resp.text
    ids = {h["hosted_id"] for h in resp.json()["hosted"]}
    assert ids == {"h1", "h2"}


def test_get_agent_returns_200_for_a_known_id() -> None:
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.get("/hosted-agents/h1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["hosted_id"] == "h1"


def test_get_agent_404_for_an_unknown_id() -> None:
    client = TestClient(_app(FakeHostedManager()))
    resp = client.get("/hosted-agents/does-not-exist")
    assert resp.status_code == 404, resp.text
    assert "does-not-exist" in resp.text


def test_stop_agent_200_when_found() -> None:
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.delete("/hosted-agents/h1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stopped": "h1"}
    assert mgr.get("h1") is None


def test_stop_agent_404_when_not_found() -> None:
    client = TestClient(_app(FakeHostedManager()))
    resp = client.delete("/hosted-agents/does-not-exist")
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------- #
# POST /hosted-agents/{id}/invoke
# --------------------------------------------------------------------------- #


def _patch_httpx_transport(monkeypatch, handler) -> None:
    """Swap api.hosted's ``httpx`` reference for a MockTransport-backed
    client, same technique test_engine_run.py uses for the engine module —
    kept as a real ``httpx.HTTPStatusError`` since the route's except clause
    still names it."""
    transport = httpx.MockTransport(handler)

    def _client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(
        hosted_api_mod, "httpx",
        SimpleNamespace(AsyncClient=_client, HTTPStatusError=httpx.HTTPStatusError),
    )


class _RaisingAsyncClient:
    """Stand-in for httpx.AsyncClient whose ``post`` raises before ever
    getting a response — exercises the route's generic-exception branch."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> "_RaisingAsyncClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        raise self._exc


def test_invoke_success_returns_the_peer_result(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["capability"] == "analyze.data"
        return httpx.Response(200, json={"analyzed": True})

    _patch_httpx_transport(monkeypatch, handler)
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1", port=9100))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/invoke", json={
        "peer_port": 9200, "capability": "analyze.data", "payload": {"x": 1},
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": {"analyzed": True}}


def test_invoke_404_for_an_unknown_hosted_id() -> None:
    client = TestClient(_app(FakeHostedManager()))
    resp = client.post("/hosted-agents/does-not-exist/invoke", json={
        "peer_port": 9200, "capability": "analyze.data",
    })
    assert resp.status_code == 404, resp.text


def test_invoke_peer_http_error_maps_to_502(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="peer blew up")

    _patch_httpx_transport(monkeypatch, handler)
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/invoke", json={
        "peer_port": 9200, "capability": "analyze.data",
    })
    assert resp.status_code == 502, resp.text
    assert "invoke failed (500)" in resp.text
    assert "peer blew up" in resp.text


def test_invoke_connection_failure_maps_to_502(monkeypatch) -> None:
    monkeypatch.setattr(
        hosted_api_mod, "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kw: _RaisingAsyncClient(RuntimeError("connection refused")),
            HTTPStatusError=httpx.HTTPStatusError,
        ),
    )
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/invoke", json={
        "peer_port": 9200, "capability": "analyze.data",
    })
    assert resp.status_code == 502, resp.text
    assert "invoke failed" in resp.text
    assert "connection refused" in resp.text


# --------------------------------------------------------------------------- #
# POST /hosted-agents/{id}/resolve-and-handshake — the remaining branches
# not already covered by test_federation.py (unknown id, did:web resolution
# failure, and the admin dial's two error shapes).
# --------------------------------------------------------------------------- #


def test_resolve_and_handshake_404_for_an_unknown_hosted_id() -> None:
    client = TestClient(_app(FakeHostedManager()))
    resp = client.post("/hosted-agents/does-not-exist/resolve-and-handshake", json={
        "peer_did": "did:web:org-c.aitp.test",
    })
    assert resp.status_code == 404, resp.text


def test_resolve_and_handshake_success_returns_established_trust(monkeypatch) -> None:
    async def _fake_resolve(peer_did: str) -> str:
        return "https://org-c.aitp.test/.well-known/aitp-manifest"

    monkeypatch.setattr(hosted_api_mod, "resolve_did_web", _fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["peer_manifest_url"] == "https://org-c.aitp.test/.well-known/aitp-manifest"
        assert body["requested_grants"] == ["analyze.data"]
        return httpx.Response(200, json={
            "grants": ["analyze.data"], "peer_aid": "aid-peer",
            "peer_port": 9200, "session_id": "sess-1", "jti": "jti-1",
        })

    _patch_httpx_transport(monkeypatch, handler)
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/resolve-and-handshake", json={
        "peer_did": "did:web:org-c.aitp.test",
        "requested_grants": ["analyze.data"],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trust"] == "established"
    assert body["peer_did"] == "did:web:org-c.aitp.test"
    assert body["resolved_manifest_url"] == "https://org-c.aitp.test/.well-known/aitp-manifest"
    assert body["peer_origin"] == "https://org-c.aitp.test"
    assert body["peer_base_url"] == "https://org-c.aitp.test"
    assert body["grants"] == ["analyze.data"]
    assert body["peer_aid"] == "aid-peer"
    assert body["jti"] == "jti-1"


def test_resolve_and_handshake_502_when_did_web_resolution_raises(monkeypatch) -> None:
    async def _fake_resolve(peer_did: str) -> str:
        raise RuntimeError("DNS lookup failed")

    monkeypatch.setattr(hosted_api_mod, "resolve_did_web", _fake_resolve)
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/resolve-and-handshake", json={
        "peer_did": "did:web:org-c.aitp.test",
    })
    assert resp.status_code == 502, resp.text
    assert "did:web resolution failed" in resp.text
    assert "DNS lookup failed" in resp.text


def test_resolve_and_handshake_502_when_the_admin_dial_returns_an_http_error(monkeypatch) -> None:
    async def _fake_resolve(peer_did: str) -> str:
        return "https://org-c.aitp.test/.well-known/aitp-manifest"

    monkeypatch.setattr(hosted_api_mod, "resolve_did_web", _fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="handshake blew up")

    _patch_httpx_transport(monkeypatch, handler)
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/resolve-and-handshake", json={
        "peer_did": "did:web:org-c.aitp.test",
    })
    assert resp.status_code == 502, resp.text
    assert "handshake failed (500)" in resp.text
    assert "handshake blew up" in resp.text


def test_resolve_and_handshake_502_when_the_admin_dial_raises(monkeypatch) -> None:
    async def _fake_resolve(peer_did: str) -> str:
        return "https://org-c.aitp.test/.well-known/aitp-manifest"

    monkeypatch.setattr(hosted_api_mod, "resolve_did_web", _fake_resolve)
    monkeypatch.setattr(
        hosted_api_mod, "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kw: _RaisingAsyncClient(RuntimeError("connection refused")),
            HTTPStatusError=httpx.HTTPStatusError,
        ),
    )
    mgr = FakeHostedManager()
    mgr.seed(_hosted_agent("h1"))
    client = TestClient(_app(mgr))
    resp = client.post("/hosted-agents/h1/resolve-and-handshake", json={
        "peer_did": "did:web:org-c.aitp.test",
    })
    assert resp.status_code == 502, resp.text
    assert "handshake failed" in resp.text
    assert "connection refused" in resp.text
