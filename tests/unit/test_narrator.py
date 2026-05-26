"""Narrator output is the documented user-visible string format —
test each event type that maps to a line."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aitp_playground.main import create_app
from aitp_playground.observability.narrator import narrate_event, narrate_events


def test_unknown_event_returns_empty_string() -> None:
    assert narrate_event({"type": "not.real"}) == ""
    assert narrate_event({}) == ""


def test_run_lifecycle_lines() -> None:
    assert narrate_event({"type": "run.started", "scenario_ref": "x@1"}).startswith("[run] started")
    assert narrate_event({"type": "run.complete"}) == "[run] complete"
    assert "FAILED" in narrate_event({"type": "run.failed", "error": "oops"})


def test_trust_established_includes_grants_and_jti() -> None:
    line = narrate_event({
        "type": "trust.established",
        "initiator": "writer",
        "target": "researcher",
        "grants": ["research.query"],
        "jti": "abcdef1234567890",
    })
    assert "writer" in line and "researcher" in line
    assert "research.query" in line
    assert "abcdef" in line


def test_delegation_chain_lines() -> None:
    issuing = narrate_event({
        "type": "delegation.issuing",
        "initiator": "researcher",
        "target": "sub-researcher",
        "grants": ["write.content"],
    })
    assert "issuing" in issuing and "researcher -> sub-researcher" in issuing
    assert narrate_event({"type": "delegation.redeemed"}).startswith("[delegate]")


def test_revocation_lines_distinguish_local_vs_cp() -> None:
    local = narrate_event({"type": "tct.revoked", "jti": "j"})
    cp = narrate_event({"type": "revocation.published", "jti": "j", "result": {"to_cp": True}})
    assert "local deny-set" in local
    assert "published to CP" in cp


def test_fault_injection_lines() -> None:
    inj = narrate_event({
        "type": "step.fault_injected",
        "step_id": "s1",
        "target": "writer",
        "notes": "kind=manifest_404 note=demo",
    })
    assert "INJECTED" in inj and "writer" in inj
    complete = narrate_event({
        "type": "step.fault_complete",
        "step_id": "s1",
        "result": {"error": "ConnectionRefusedError: ..."},
    })
    assert "complete" in complete and "ConnectionRefusedError" in complete


def test_narrate_events_drops_unknowns() -> None:
    events = [
        {"type": "run.started", "scenario_ref": "x@1"},
        {"type": "not.real"},
        {"type": "run.complete"},
    ]
    lines = narrate_events(events)
    assert len(lines) == 2
    assert lines[0].startswith("[run] started")
    assert lines[-1] == "[run] complete"


def test_narrate_endpoint_returns_text() -> None:
    c = TestClient(create_app())
    posted = c.post("/runs", json={
        "scenario_ref": "intra-org/trust-gate@1.0.0",
        "inputs": {"topic": "demo"},
    }).json()
    rid = posted["run_id"]
    r = c.get(f"/runs/{rid}/narrate")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # The trailing summary line is always present.
    assert "status=" in r.text and "events=" in r.text


def test_narrate_unknown_run_returns_404() -> None:
    c = TestClient(create_app())
    r = c.get("/runs/does-not-exist/narrate")
    assert r.status_code == 404
