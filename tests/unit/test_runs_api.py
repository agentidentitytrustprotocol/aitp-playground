"""Smoke tests for the async runs API surface."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aitp_playground.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_post_runs_returns_202_with_run_id() -> None:
    c = _client()
    r = c.post("/runs", json={
        "scenario_ref": "intra-org/trust-gate@1.0.0",
        "inputs": {"topic": "demo"},
    })
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["scenario_ref"] == "intra-org/trust-gate@1.0.0"
    assert isinstance(body["run_id"], str) and len(body["run_id"]) > 0


def test_list_runs_includes_recently_created() -> None:
    c = _client()
    posted = c.post("/runs", json={
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "inputs": {"topic": "ai"},
    }).json()
    listing = c.get("/runs").json()
    assert any(r["run_id"] == posted["run_id"] for r in listing["runs"])


def test_get_run_status_returns_state() -> None:
    c = _client()
    posted = c.post("/runs", json={
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "inputs": {"topic": "ai"},
    }).json()
    rid = posted["run_id"]
    s = c.get(f"/runs/{rid}/status")
    assert s.status_code == 200
    j = s.json()
    assert j["run_id"] == rid
    assert j["status"] in {"pending", "running", "failed", "success", "cancelled"}
    assert "event_count" in j


def test_cancel_unknown_run_is_404() -> None:
    r = _client().post("/runs/does-not-exist/cancel")
    assert r.status_code == 404


def test_new_scenarios_are_loadable() -> None:
    """trust-gate, scoped-capabilities, and revocation-demo all register."""
    c = _client()
    expected = {
        "intra-org/trust-gate@1.0.0": "Trust Gate — Access Denied Then Granted",
        "intra-org/scoped-capabilities@1.0.0": "Scoped Capabilities — Grant Intersection",
        "intra-org/revocation-demo@1.0.0": "Revocation Demo (RFC-AITP-0008)",
        "intra-org/delegation-chain@1.0.0": "Delegation Chain (RFC-AITP-0006)",
    }
    for ref, name in expected.items():
        r = c.get(f"/scenarios/{ref}")
        assert r.status_code == 200, f"{ref} did not load: {r.text}"
        assert r.json()["metadata"]["name"] == name
