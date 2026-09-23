"""Unit tests for `AitpServer`'s live-wire handshake routes
(agents/base/aitp_server.py): the HELLO_ACK responder (`/aitp/handshake/hello`)
and COMMIT responder (`/aitp/handshake/commit`), plus `/admin/rotate-keys` and
the did:web document route. Before this, all four were exercised only by
spawning a real agent subprocess in the gated e2e suite.

`test_manifest_verification.py` and `test_delegation_revocation.py` already
build `AitpServer` instances and drive `process_hello`/`process_commit`
directly on the SDK session objects; this file goes one layer further and
drives the actual FastAPI ROUTE handlers via `TestClient`, which is what is
actually uncovered — session-state mutation, the emitted telemetry events, and
the HTTP status/shape of a rejected handshake.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

from aitp_server import AitpServer  # noqa: E402


def _server(**kwargs: Any) -> AitpServer:
    agent = aitp.AitpAgent.generate()
    manifest = agent.build_manifest(
        display_name="responder",
        handshake_endpoint="http://localhost:9/aitp/handshake/hello",
        offered_caps=["demo.write"],
    )
    bootstrap = {
        "run_id": "r",
        "agent_id": "responder",
        "aitp": {
            "seed_hex": "55" * 32,
            "display_name": "responder",
            "handshake_endpoint": "http://localhost:9/aitp/handshake/hello",
            "offered_caps": ["demo.write"],
        },
    }
    return AitpServer(agent=agent, manifest_json=manifest, port=9, bootstrap=bootstrap, **kwargs)


def _client(server: AitpServer) -> TestClient:
    app = FastAPI()
    app.include_router(server.router)
    return TestClient(app)


def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def _capture(event_type, _bootstrap, **fields):
        captured.append({"type": event_type, **fields})

    import aitp_server as mod

    monkeypatch.setattr(mod, "emit_event", _capture)
    return captured


def _initiator_hello(server: AitpServer, *, grants: list[str] | None = None):
    """A real initiator agent building a real HELLO against `server`."""
    initiator = aitp.AitpAgent.generate()
    initiator.build_manifest(
        display_name="initiator",
        handshake_endpoint="http://localhost:10/aitp/handshake/hello",
        offered_caps=["demo.read"],
    )
    session = initiator.new_session()
    hello = session.build_hello(server.manifest_json, grants or ["demo.write"])
    return initiator, session, hello


# ── /aitp/handshake/hello (HELLO_ACK responder) ──────────────────────────


def test_hello_route_success_creates_a_session_and_emits_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    events = _events(monkeypatch)
    initiator, _session, hello = _initiator_hello(server)

    resp = _client(server).post("/aitp/handshake/hello", content=hello)

    assert resp.status_code == 200, resp.text
    session_id = resp.headers["x-aitp-session-id"]
    assert session_id
    json.loads(resp.text)  # a real HELLO_ACK envelope, not an error shape
    assert session_id in server._sessions, "the responder session was not stored"

    started = [e for e in events if e["type"] == "handshake.started"]
    assert len(started) == 1
    assert started[0]["session_id"] == session_id
    assert started[0]["role"] == "responder"


def test_hello_route_rejects_a_bad_identity_proof_and_emits_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tampered proof-of-possession signature — the live-wire equivalent of
    an attacker who does not hold the claimed private key. This is the
    HELLO_ACK responder's own rejection path, not the SDK's in isolation."""
    server = _server()
    events = _events(monkeypatch)
    _initiator, _session, hello = _initiator_hello(server)

    envelope = json.loads(hello)
    good_proof = envelope["payload"]["identity"]["proof"]
    envelope["payload"]["identity"]["proof"] = ("A" if good_proof[0] != "A" else "B") + good_proof[1:]
    tampered = json.dumps(envelope)

    resp = _client(server).post("/aitp/handshake/hello", content=tampered)

    assert resp.status_code == 400, resp.text
    assert "signature" in resp.json()["error"].lower()
    assert server._sessions == {}, "a rejected HELLO must not leave a session behind"

    failed = [e for e in events if e["type"] == "handshake.failed"]
    assert len(failed) == 1
    assert "signature" in failed[0]["error"].lower()


# ── /aitp/handshake/commit (COMMIT responder) ────────────────────────────


def test_commit_route_success_completes_and_emits_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    events = _events(monkeypatch)
    initiator, session, hello = _initiator_hello(server)

    hello_resp = _client(server).post("/aitp/handshake/hello", content=hello)
    session_id = hello_resp.headers["x-aitp-session-id"]
    commit = session.process_hello_ack(hello_resp.text, session_id)

    resp = _client(server).post(
        "/aitp/handshake/commit",
        content=commit,
        headers={"X-Aitp-Session-Id": session_id},
    )

    assert resp.status_code == 200, resp.text
    json.loads(resp.text)  # a real ack, not an error shape
    assert session_id not in server._sessions, "a completed session must be popped"

    completed = [e for e in events if e["type"] == "handshake.complete"]
    assert len(completed) == 1
    assert completed[0]["session_id"] == session_id
    assert completed[0]["role"] == "responder"
    assert completed[0]["peer_aid"] == initiator.aid
    # `completed["tct"]` is the token the INITIATOR issued to us (RFC-AITP-0006
    # is mutual), so its grants are the initiator's own offered capabilities,
    # not what the initiator requested from us.
    assert completed[0]["grants"] == ["demo.read"]


def test_commit_route_with_an_unknown_session_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    events = _events(monkeypatch)

    resp = _client(server).post(
        "/aitp/handshake/commit",
        content="{}",
        headers={"X-Aitp-Session-Id": "does-not-exist"},
    )

    assert resp.status_code == 404, resp.text
    assert "does-not-exist" in resp.text
    # A missing session is a caller-state 404, not a protocol failure — must
    # not be reported as a handshake rejection.
    assert not [e for e in events if e["type"] == "handshake.failed"]


def test_commit_route_with_a_malformed_body_is_rejected_and_consumes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session is popped BEFORE `process_commit` is attempted (it is a
    responder-side, single-use object), so a failed commit still consumes
    it — a retry with the correct commit bytes must now see 404, not a
    second chance."""
    server = _server()
    events = _events(monkeypatch)
    _initiator, _session, hello = _initiator_hello(server)

    hello_resp = _client(server).post("/aitp/handshake/hello", content=hello)
    session_id = hello_resp.headers["x-aitp-session-id"]
    client = _client(server)

    bad_resp = client.post(
        "/aitp/handshake/commit",
        content="not a real commit envelope",
        headers={"X-Aitp-Session-Id": session_id},
    )
    assert bad_resp.status_code == 400, bad_resp.text
    assert session_id not in server._sessions

    failed = [e for e in events if e["type"] == "handshake.failed"]
    assert len(failed) == 1
    assert failed[0]["session_id"] == session_id

    retry = client.post(
        "/aitp/handshake/commit",
        content="not a real commit envelope",
        headers={"X-Aitp-Session-Id": session_id},
    )
    assert retry.status_code == 404, "the session must have been consumed by the first attempt"


# ── /admin/rotate-keys ────────────────────────────────────────────────────


def test_rotate_keys_replaces_identity_and_drops_pending_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server()
    events = _events(monkeypatch)
    old_aid = server.agent.aid
    old_manifest_json = server.manifest_json

    _initiator, _session, hello = _initiator_hello(server)
    hello_resp = _client(server).post("/aitp/handshake/hello", content=hello)
    session_id = hello_resp.headers["x-aitp-session-id"]
    assert session_id in server._sessions

    client = _client(server)
    resp = client.post("/admin/rotate-keys")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old_aid"] == old_aid
    assert body["aid"] == server.agent.aid
    assert body["aid"] != old_aid
    assert body["manifest_replaced"] is True

    assert server.manifest_json != old_manifest_json
    aitp.verify_manifest_json(server.manifest_json)  # the new manifest is self-consistent
    assert json.loads(server.manifest_json)["manifest"]["aid"] == server.agent.aid

    assert server._sessions == {}, "an in-flight session under the old key must be dropped"
    # And the dropped session cannot be committed against — it was scoped to
    # a responder built from the now-replaced agent/key.
    commit_after_rotation = client.post(
        "/aitp/handshake/commit",
        content="{}",
        headers={"X-Aitp-Session-Id": session_id},
    )
    assert commit_after_rotation.status_code == 404

    rotated = [e for e in events if e["type"] == "identity.key.rotated"]
    assert len(rotated) == 1
    assert rotated[0]["old_aid"] == old_aid
    assert rotated[0]["new_aid"] == server.agent.aid


# ── did:web document route ────────────────────────────────────────────────


def test_did_web_document_route_present_when_configured() -> None:
    server = _server(did_web_host="agent.example.com", did_web_scheme="https")
    resp = _client(server).get("/.well-known/did.json")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/did+json")
    doc = resp.json()
    assert doc["id"] == "did:web:agent.example.com"
    assert doc["service"][0]["serviceEndpoint"] == "https://agent.example.com"
    assert doc["service"][0]["type"] == "AitpManifest"


def test_did_web_document_route_absent_without_a_configured_host() -> None:
    """The route is registered conditionally, not just guarded at runtime —
    an agent with no did:web host must not advertise the endpoint at all."""
    server = _server()
    resp = _client(server).get("/.well-known/did.json")
    assert resp.status_code == 404
