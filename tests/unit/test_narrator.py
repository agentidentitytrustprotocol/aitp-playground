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


def test_agent_lifecycle_lines() -> None:
    spawning = narrate_event({"type": "agent.spawning", "agent_id": "writer", "port": 8101})
    assert "spawning writer" in spawning and "8101" in spawning
    ready = narrate_event({
        "type": "agent.ready", "agent_id": "writer",
        "aid": "aitp:key:z6MkabcdefghijklmnopqrstuvwxyzABCDEF", "port": 8101,
    })
    assert "ready" in ready and "writer" in ready
    # Long AIDs are truncated with an ellipsis, not dumped raw.
    assert "…" in ready


def test_trust_flow_lines() -> None:
    resolved = narrate_event({
        "type": "trust.peers_resolved",
        "peers": {"a": "http://x", "b": "http://y"},
    })
    assert "resolved (2)" in resolved
    establishing = narrate_event({
        "type": "trust.establishing", "initiator": "writer", "target": "researcher",
    })
    assert "handshaking" in establishing and "writer -> researcher" in establishing
    # No grants listed → the <all-offered> marker.
    all_offered = narrate_event({
        "type": "trust.established", "initiator": "a", "target": "b", "jti": "j",
    })
    assert "<all-offered>" in all_offered
    failed = narrate_event({"type": "handshake.failed", "error": "sig mismatch"})
    assert "FAILED handshake" in failed and "sig mismatch" in failed
    assert narrate_event({"type": "run.cancelled"}) == "[run] cancelled"


def test_probe_and_step_lines() -> None:
    assert "starting" in narrate_event({
        "type": "step.started", "step_id": "s1", "agent": "writer", "capability": "write.content",
    })
    assert "skipped" in narrate_event({
        "type": "step.skipped", "step_id": "s1", "notes": "CP not configured",
    })
    no_trust = narrate_event({
        "type": "step.probing_no_trust",
        "initiator": "writer", "target": "researcher", "capability": "research.query",
    })
    assert "no-trust call" in no_trust
    with_tct = narrate_event({
        "type": "step.probing_with_held_tct",
        "initiator": "writer", "target": "researcher", "capability": "research.query",
    })
    assert "with-TCT call" in with_tct
    denied = narrate_event({
        "type": "step.access_denied", "step_id": "s1",
        "capability": "write.content", "result": {"status_code": 403},
    })
    assert "DENIED" in denied and "status=403" in denied
    unexpected = narrate_event({
        "type": "step.unexpected_status", "step_id": "s1", "result": {"status_code": 500},
    })
    assert "UNEXPECTED" in unexpected and "status=500" in unexpected


def test_step_complete_variants() -> None:
    with_cap = narrate_event({
        "type": "step.complete", "step_id": "s1",
        "agent": "writer", "capability": "write.content",
    })
    assert "writer.write.content" in with_cap
    revoke = narrate_event({
        "type": "step.complete", "step_id": "rev", "result": {"revoked_jti": "j"},
    })
    assert "revoke_tct" in revoke
    delegate = narrate_event({
        "type": "step.complete", "step_id": "del", "result": {"delegatee_aid": "aid"},
    })
    assert "delegate" in delegate
    bare = narrate_event({"type": "step.complete", "step_id": "hs"})
    assert bare == "[step]  ok       id=hs"


def test_cp_enrollment_and_webhook_lines() -> None:
    assert "enrolling writer" in narrate_event({
        "type": "cp.enroll_started", "agent_id": "writer",
    })
    assert "enrolled" in narrate_event({
        "type": "cp.enroll_complete", "agent_id": "writer", "aid": "aid",
    })
    failed = narrate_event({
        "type": "cp.enroll_failed", "stage": "register", "error": "409",
    })
    assert "enroll FAILED (register)" in failed
    assert "registered writer" in narrate_event({
        "type": "cp.enroll_succeeded", "agent_id": "writer",
    })
    subscribed = narrate_event({
        "type": "cp.webhook.subscribed",
        "result": {"id": "wh-1", "events": ["tct.revoked"]},
    })
    assert "webhook subscribed" in subscribed and "tct.revoked" in subscribed
    all_events = narrate_event({
        "type": "cp.webhook.subscribed", "result": {"id": "wh-1", "events": []},
    })
    assert "<all>" in all_events
    assert "subscribe FAILED" in narrate_event({
        "type": "cp.webhook.subscribe_failed", "notes": "CP unreachable",
    })
    delivered = narrate_event({
        "type": "cp.webhook.delivered",
        "event_type": "handshake.complete", "delivery_id": "d-1",
    })
    assert "webhook delivery" in delivered and "handshake.complete" in delivered


def test_revocation_refresh_lines() -> None:
    fetched = narrate_event({
        "type": "revocation.list_fetched", "jti_count": 3, "added": 1,
    })
    assert "jti_count=3" in fetched and "added=1" in fetched
    assert "refresh FAILED" in narrate_event({
        "type": "revocation.refresh_failed", "error": "timeout",
    })
    assert "issued" in narrate_event({"type": "delegation.issued"})
    assert "redeeming" in narrate_event({
        "type": "delegation.redeeming", "initiator": "a", "target": "b",
    })
    assert "REJECTED" in narrate_event({
        "type": "delegation.rejected", "error": "expired",
    })


def test_identity_renewal_and_oidc_lines() -> None:
    rotated = narrate_event({
        "type": "identity.key.rotated", "agent_id": "writer", "aid": "aid-new",
    })
    assert "writer rotated keys" in rotated
    minted = narrate_event({
        "type": "oidc.issuer_minted", "result": {"issuer_url": "http://iss"},
    })
    assert "per-run issuer minted" in minted and "http://iss" in minted
    renewed = narrate_event({
        "type": "tct.renewed", "agent_id": "writer", "target": "researcher", "jti": "new-jti",
    })
    assert "[renew]" in renewed and "writer <- researcher" in renewed


def test_session_bundle_and_spki_lines() -> None:
    exported = narrate_event({
        "type": "session.bundle.exported",
        "result": {"participant_aids": ["a", "b"], "session_id": "sess-1"},
    })
    assert "exported" in exported and "participants=2" in exported
    verified = narrate_event({
        "type": "session.bundle.verified",
        "result": {"kind": "session_bundle", "active_aids": ["a"]},
    })
    assert "verified" in verified and "active=1" in verified
    assert "is_pinned=True" in narrate_event({
        "type": "spki.pin.checked", "result": {"is_pinned": True},
    })


def test_self_execute_and_llm_lines() -> None:
    assert "self" in narrate_event({
        "type": "capability.self_execute", "agent_id": "r", "capability": "research.query",
    })
    assert "started" in narrate_event({"type": "llm.started", "task": "research"})
    assert "complete" in narrate_event({"type": "llm.complete", "task": "research"})


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
