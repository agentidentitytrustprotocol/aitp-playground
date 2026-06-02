"""Dashboard route test — serves a self-contained HTML page."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aitp_playground.api import dashboard as dashboard_api


def test_dashboard_serves_html() -> None:
    app = FastAPI()
    app.include_router(dashboard_api.router)
    client = TestClient(app)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # It is genuinely self-contained (no local asset deps) and wired to the
    # real APIs it consumes.
    assert "AITP" in body
    assert "/runs/" in body and "/capabilities" in body and "/cp/dashboard" in body
    assert "EventSource" in body  # live SSE stream
