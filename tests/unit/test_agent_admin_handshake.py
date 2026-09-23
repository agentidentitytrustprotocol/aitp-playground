"""Unit tests for `agent_admin.py`'s handshake-initiator and TCT-presentation
routes: `/admin/initiate-handshake`, `/admin/invoke`, `/admin/renew-tct`, and
`/admin/export-session-bundle` / `/admin/verify-session-bundle`.

Before this, `initiate_handshake`'s real peer-manifest-verify -> HELLO build
-> session-complete path had zero fast-suite coverage. `test_engine_run.py`
exercises the same HTTP surface, but through an `httpx.MockTransport` fake
that answers with canned JSON — it never runs this module's actual route
body. `test_agent_admin_routes.py` / `test_agent_admin_enroll.py` cover only
this module's own precondition and transport-failure branches, never a
successful round trip. `test_agent_admin_routes.py` also already covers
`/admin/verify-session-bundle`'s FORGED-bundle rejection; this file adds the
success path that proves the happy case actually works, not just that
tampering is caught.

Every "peer" here is a REAL `AitpServer` + admin router pair — the same
combination `agents/writer/main.py` etc. wire up — mounted as an ASGI app and
reached via `httpx.ASGITransport`, so a round trip exercises this module's
client-side code against the actual server-side route handlers, with real
signatures throughout. No protocol logic is reimplemented or mocked; only the
network hop is replaced with in-process ASGI dispatch.
"""
from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import agent_admin  # noqa: E402
from agent_admin import build_admin_router  # noqa: E402
from aitp_server import AitpServer  # noqa: E402
from revocation_state import RevocationState  # noqa: E402


def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def _capture(event_type, _bootstrap, **fields):
        captured.append({"type": event_type, **fields})

    monkeypatch.setattr(agent_admin, "emit_event", _capture)
    return captured


def _route_agent_admin_http_to(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    """Rebind `agent_admin`'s own `httpx` name to dispatch straight into
    `app` in-process. Same rebinding technique as
    `test_agent_admin_enroll.py`'s `_stub_agent_admin_transport`, but backed
    by a real ASGI app instead of a hand-written handler, so
    `initiate_handshake`'s GET (manifest) and two POSTs (hello, commit) all
    actually run the peer's real route code — not a canned response.
    """
    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))

    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )


def _peer(offered_caps: list[str], *, port: int = 9):
    """A full peer worker: `AitpServer` (manifest/hello/commit) + admin
    router (process-renewal) mounted together, exactly as
    `agents/writer/main.py` et al. wire them for real."""
    agent = aitp.AitpAgent.generate()
    endpoint = f"http://peer:{port}/aitp/handshake/hello"
    manifest_json = agent.build_manifest(
        display_name="peer", handshake_endpoint=endpoint, offered_caps=offered_caps,
    )
    bootstrap = {
        "run_id": "r", "agent_id": "peer",
        "aitp": {
            "seed_hex": "66" * 32, "display_name": "peer",
            "handshake_endpoint": endpoint, "offered_caps": offered_caps,
        },
    }
    revocation = RevocationState()
    server = AitpServer(
        agent=agent, manifest_json=manifest_json, port=port,
        bootstrap=bootstrap, revocation=revocation,
    )
    app = FastAPI()
    app.include_router(server.router)
    app.include_router(build_admin_router(
        agent=agent, bootstrap=bootstrap, held_tcts={}, revocation=revocation,
        manifest_provider=lambda: server.manifest_json,
        issued_tcts=server._issued_tcts,
    ))
    return agent, app


def _initiator(*, held_tcts: dict[int, str] | None = None):
    agent = aitp.AitpAgent.generate()
    agent.build_manifest(
        display_name="initiator",
        handshake_endpoint="http://initiator/aitp/handshake/hello",
        offered_caps=["demo.read"],
    )
    bootstrap = {"run_id": "r", "agent_id": "initiator", "aitp": {}}
    held = held_tcts if held_tcts is not None else {}
    router = build_admin_router(
        agent=agent, bootstrap=bootstrap, held_tcts=held, revocation=RevocationState(),
    )
    app = FastAPI()
    app.include_router(router)
    return agent, TestClient(app), held


# ── /admin/initiate-handshake ─────────────────────────────────────────────


def test_initiate_handshake_success_stores_a_held_tct_and_emits_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_agent, peer_app = _peer(["demo.write"])
    _route_agent_admin_http_to(monkeypatch, peer_app)
    events = _events(monkeypatch)

    _initiator_agent, client, held = _initiator()
    resp = client.post(
        "/admin/initiate-handshake",
        json={"peer_manifest_url": "http://peer:9/.well-known/aitp-manifest"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grants"] == ["demo.write"]
    assert body["peer_aid"] == peer_agent.aid
    assert body["peer_port"] == 9
    assert body["jti"]
    assert held[9], "the completed handshake's TCT must be stored under the peer's port"

    completed = [
        e for e in events if e["type"] == "handshake.complete" and e.get("role") == "initiator"
    ]
    assert len(completed) == 1
    assert completed[0]["peer_aid"] == peer_agent.aid
    assert completed[0]["session_id"] == body["session_id"]


def test_initiate_handshake_with_explicit_requested_grants_subsets_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `requested_grants` the initiator asks for everything the peer
    offers (covered above); with it, only the named subset is requested —
    the other branch of the same `if` this route uses to build `grants`."""
    peer_agent, peer_app = _peer(["demo.write", "demo.read"])
    _route_agent_admin_http_to(monkeypatch, peer_app)
    _events(monkeypatch)

    _initiator_agent, client, _held = _initiator()
    resp = client.post(
        "/admin/initiate-handshake",
        json={
            "peer_manifest_url": "http://peer:9/.well-known/aitp-manifest",
            "requested_grants": ["demo.write"],
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["grants"] == ["demo.write"]


def test_initiate_handshake_rejects_an_unverifiable_peer_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's own peer-manifest-verify step, exercised through the real
    HTTP route rather than by calling `_verify_peer_manifest` directly (as
    `test_manifest_verification.py` does)."""
    peer_agent = aitp.AitpAgent.generate()
    envelope = json.loads(peer_agent.build_manifest(
        display_name="peer", handshake_endpoint="http://peer:9/aitp/handshake/hello",
        offered_caps=["demo.write"],
    ))
    envelope["manifest"]["display_name"] = "evil"  # tamper after signing
    tampered = json.dumps(envelope)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, text=tampered)

    def factory(*_a: Any, **_k: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )
    events = _events(monkeypatch)

    _initiator_agent, client, held = _initiator()
    resp = client.post(
        "/admin/initiate-handshake",
        json={"peer_manifest_url": "http://peer:9/.well-known/aitp-manifest"},
    )

    assert resp.status_code == 502, resp.text
    assert "failed verification" in resp.text
    assert held == {}, "no TCT may be stored for a peer whose manifest did not verify"
    failures = [e for e in events if e["type"] == "manifest.verify_failed"]
    assert len(failures) == 1


# ── /admin/invoke ──────────────────────────────────────────────────────────
#
# The peer's own capability-endpoint verification is out of scope here (that
# is `AitpServer.verify_capability_tct`, covered by
# `test_delegation_revocation.py` and `test_revocation_freshness.py`); these
# tests are about THIS module's own client-side behaviour: does it present
# the held TCT and forward the payload correctly, and does a peer rejection
# come back as data rather than crash the caller.


def test_invoke_capability_returns_the_peer_body_verbatim_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/capabilities/demo.write"
        assert request.headers["x-aitp-tct"] == "held-token"
        assert json.loads(request.content) == {"topic": "aitp"}
        return httpx.Response(200, json={"findings": "ok"})

    def factory(*_a: Any, **_k: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )

    _agent, client, _held = _initiator(held_tcts={9999: "held-token"})
    resp = client.post(
        "/admin/invoke",
        json={"peer_port": 9999, "capability": "demo.write", "payload": {"topic": "aitp"}},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"findings": "ok"}


def test_invoke_capability_reports_a_peer_rejection_as_data_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 from the peer (e.g. a revoked TCT) must not blow up the caller —
    the playground's probe step needs to observe the rejection."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "tct rejected: revoked"})

    def factory(*_a: Any, **_k: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )

    _agent, client, _held = _initiator(held_tcts={9999: "held-token"})
    resp = client.post("/admin/invoke", json={"peer_port": 9999, "capability": "demo.write"})

    assert resp.status_code == 200, "the ROUTE succeeds; the rejection is reported as data"
    body = resp.json()
    assert body == {
        "error": True,
        "status_code": 403,
        "body": {"detail": "tct rejected: revoked"},
    }


# ── /admin/renew-tct ───────────────────────────────────────────────────────


def test_renew_tct_without_a_held_tct_is_412() -> None:
    _agent, client, _held = _initiator()
    resp = client.post("/admin/renew-tct", json={"peer_port": 9999})
    assert resp.status_code == 412, resp.text
    assert "no tct held" in resp.text.lower()


def test_renew_tct_success_swaps_the_held_tct_for_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_agent, peer_app = _peer(["demo.write"])
    _route_agent_admin_http_to(monkeypatch, peer_app)
    events = _events(monkeypatch)

    initiator_agent, client, held = _initiator()
    handshake = client.post(
        "/admin/initiate-handshake",
        json={"peer_manifest_url": "http://peer:9/.well-known/aitp-manifest"},
    ).json()
    peer_port = handshake["peer_port"]
    original_jti = handshake["jti"]
    original_token = held[peer_port]

    resp = client.post("/admin/renew-tct", json={"peer_port": peer_port})

    assert resp.status_code == 200, resp.text
    renewed = resp.json()
    assert renewed["issuer"] == peer_agent.aid
    assert renewed["subject"] == initiator_agent.aid
    assert renewed["jti"] != original_jti
    assert held[peer_port] != original_token, "the held TCT must be swapped to the renewed one"

    requested = [e for e in events if e["type"] == "tct.renewal.requested"]
    assert len(requested) == 1 and requested[0]["peer_port"] == peer_port
    issued = [e for e in events if e["type"] == "tct.renewal.issued"]
    assert len(issued) == 1


# ── /admin/export-session-bundle + /admin/verify-session-bundle ──────────


def test_export_and_verify_session_bundle_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The success complement to `test_agent_admin_routes.py`'s tampered-bundle
    test. Uses the `participant_tcts` override the export route's own
    docstring names as the test-fixture path (the alternative,
    `issued_tcts`-derived path needs a responder session cache this
    router-only harness does not have).

    Verification is done from the PARTICIPANT's own admin router, not the
    coordinator's: `aitp.verify_session_bundle` checks that the verifying
    agent is itself a bundle member, and the coordinator who exported the
    bundle never added itself as a participant — exactly as a real
    RFC-AITP-0010 flow works (the coordinator issues it; a participant
    checks who else is still active).
    """
    events = _events(monkeypatch)

    coordinator = aitp.AitpAgent.generate()
    coord_manifest = coordinator.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    participant = aitp.AitpAgent.generate()
    participant.build_manifest("p", "http://p/aitp/handshake/hello", ["x"])

    # A real handshake, so the TCT the bundle carries is genuine.
    sess = participant.new_session()
    rsess = coordinator.new_responder()
    hello = sess.build_hello(coord_manifest, ["session.member"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    participant_tct = json.loads(sess.complete(commit_ack))["tct"]

    coord_router = build_admin_router(
        agent=coordinator,
        bootstrap={"run_id": "r", "agent_id": "coord", "aitp": {}},
        held_tcts={}, revocation=RevocationState(),
    )
    coord_app = FastAPI()
    coord_app.include_router(coord_router)
    coord_client = TestClient(coord_app)

    export_resp = coord_client.post(
        "/admin/export-session-bundle",
        json={"participant_tcts": [{"aid": participant.aid, "tct_token": participant_tct}]},
    )
    assert export_resp.status_code == 200, export_resp.text
    body = export_resp.json()
    assert body["participant_aids"] == [participant.aid]
    assert uuid.UUID(body["session_id"])  # a real uuid4, not a placeholder

    exported = [e for e in events if e["type"] == "session.bundle.exported"]
    assert len(exported) == 1
    assert exported[0]["participant_count"] == 1

    participant_router = build_admin_router(
        agent=participant,
        bootstrap={"run_id": "r", "agent_id": "p", "aitp": {}},
        held_tcts={}, revocation=RevocationState(),
    )
    participant_app = FastAPI()
    participant_app.include_router(participant_router)
    participant_client = TestClient(participant_app)

    verify_resp = participant_client.post(
        "/admin/verify-session-bundle", json={"bundle_envelope": body["bundle_envelope"]},
    )
    assert verify_resp.status_code == 200, verify_resp.text
    outcome = verify_resp.json()
    assert participant.aid in outcome["active_aids"]
    assert outcome["dropped_aids"] == []

    verified = [e for e in events if e["type"] == "session.bundle.verified"]
    assert len(verified) == 1
    assert verified[0]["active_count"] == 1
