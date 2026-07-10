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


def test_run_label_is_echoed_on_create_list_and_detail() -> None:
    c = _client()
    posted = c.post("/runs", json={
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "inputs": {"topic": "ai"},
        "run_label": "nightly-smoke",
    })
    assert posted.status_code == 202, posted.text
    created = posted.json()
    assert created["run_label"] == "nightly-smoke"
    rid = created["run_id"]

    summary = next(r for r in c.get("/runs").json()["runs"] if r["run_id"] == rid)
    assert summary["run_label"] == "nightly-smoke"

    detail = c.get(f"/runs/{rid}").json()
    assert detail["run_label"] == "nightly-smoke"


def test_run_without_label_echoes_null() -> None:
    c = _client()
    rid = c.post("/runs", json={
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "inputs": {"topic": "ai"},
    }).json()["run_id"]
    assert c.get(f"/runs/{rid}").json()["run_label"] is None


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


def test_cp_audit_proxy_disabled_when_cp_unset(monkeypatch) -> None:
    """With no CP_BASE_URL the proxy must return cp_enabled=False and
    an empty events list — callers branch on cp_enabled instead of
    silently treating the lack of CP as a successful query."""
    monkeypatch.setenv("CP_BASE_URL", "")
    monkeypatch.setenv("CP_API_KEY", "")
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    c = TestClient(create_app())
    posted = c.post("/runs", json={
        "scenario_ref": "intra-org/trust-gate@1.0.0",
        "inputs": {"topic": "demo"},
    }).json()
    rid = posted["run_id"]
    r = c.get(f"/runs/{rid}/cp-audit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cp_enabled"] is False
    assert body["events"] == []
    assert body["count"] == 0


def test_cp_audit_proxy_404_for_unknown_run() -> None:
    r = _client().get("/runs/does-not-exist/cp-audit")
    assert r.status_code == 404


def test_cp_sessions_proxy_disabled_when_cp_unset(monkeypatch) -> None:
    monkeypatch.setenv("CP_BASE_URL", "")
    monkeypatch.setenv("CP_API_KEY", "")
    from aitp_playground import config
    monkeypatch.setattr(config, "_settings", None)
    c = TestClient(create_app())
    posted = c.post("/runs", json={
        "scenario_ref": "intra-org/trust-gate@1.0.0",
        "inputs": {"topic": "demo"},
    }).json()
    rid = posted["run_id"]
    r = c.get(f"/runs/{rid}/cp-sessions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cp_enabled"] is False
    assert body["sessions"] == []


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
