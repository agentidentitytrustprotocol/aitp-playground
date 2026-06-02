"""Route tests for the /cp observability proxies.

The enabled-CP HTTP path is covered at the client level in test_cp_client.py
(httpx MockTransport). Here we assert the routes wire up and degrade cleanly
to ``cp_enabled=false`` with empty payloads when no CP is configured — the
contract a CLI / dashboard relies on to call them unconditionally.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aitp_playground.api import cp as cp_api
from aitp_playground.config import Settings
from aitp_playground.cp_client.client import CpClient


def _client_no_cp() -> TestClient:
    app = FastAPI()
    app.state.cp = CpClient(Settings(cp_base_url=""))  # disabled
    app.include_router(cp_api.router)
    return TestClient(app)


def test_list_endpoints_degrade_to_empty() -> None:
    client = _client_no_cp()
    for path in (
        "/cp/tcts",
        "/cp/delegations",
        "/cp/agents",
        "/cp/trust-anchors",
        "/cp/pinned-keys",
        "/cp/sessions/s-1/replay",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        body = resp.json()
        assert body["cp_enabled"] is False, path
        assert body["items"] == [], path
        assert body["count"] == 0, path


def test_dashboard_degrades_to_empty_object() -> None:
    client = _client_no_cp()
    resp = client.get("/cp/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cp_enabled"] is False
    assert body["data"] == {}


def test_tcts_route_forwards_filters_to_client(monkeypatch) -> None:
    """When CP is enabled, the route hands its query params to the client."""
    captured: dict = {}

    app = FastAPI()
    cp = CpClient(Settings(cp_base_url="http://cp"))

    async def _fake_fetch_tcts(**kwargs):
        captured.update(kwargs)
        return [{"jti": "t1"}]

    cp.fetch_tcts = _fake_fetch_tcts  # type: ignore[method-assign]
    app.state.cp = cp
    app.include_router(cp_api.router)
    client = TestClient(app)

    resp = client.get("/cp/tcts", params={"capability": "write.content", "active": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cp_enabled"] is True
    assert body["items"] == [{"jti": "t1"}]
    assert captured["capability"] == "write.content"
    assert captured["active"] is True
