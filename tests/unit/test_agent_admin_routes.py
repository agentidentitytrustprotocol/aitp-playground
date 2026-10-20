"""Route-level rejection branches in `agent_admin.py` — untested before this
file: every non-2xx status this module raises directly (as opposed to one
mirrored from a peer's response) had zero coverage. Each of these guards is
the module's own precondition or wiring check, not a downstream failure —
`DECISIONS.md` D-10's taxonomy names them 412 (caller state), 404 (unknown
capability), 500 (wiring bug here).

Also covers `/admin/verify-session-bundle`'s forged-bundle and
wrong-coordinator rejections (403) and its missing-Manifest guard (400).
"""
from __future__ import annotations

import json
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import agent_admin  # noqa: E402
from agent_admin import build_admin_router  # noqa: E402
from revocation_state import RevocationState  # noqa: E402


def _app(*, manifest_provider=None, capabilities=None) -> tuple[FastAPI, Any]:
    """A bare admin router mounted alone — these guards fire before any AITP
    protocol state (sessions, handshakes) is needed, so `AitpServer` itself
    is out of scope here, unlike `test_delegation_revocation.py`'s harness.
    """
    agent = aitp.AitpAgent.generate()
    bootstrap = {"run_id": "r", "agent_id": "a", "aitp": {}}
    router = build_admin_router(
        agent=agent,
        bootstrap=bootstrap,
        held_tcts={},
        revocation=RevocationState(),
        manifest_provider=manifest_provider,
        capabilities=capabilities,
    )
    app = FastAPI()
    app.include_router(router)
    return app, agent


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# --- 412: caller-state preconditions, no TCT/voucher held for the port ---


def test_invoke_without_a_held_tct_is_412() -> None:
    app, _agent = _app()
    resp = _client(app).post(
        "/admin/invoke", json={"peer_port": 9999, "capability": "demo.x"},
    )
    assert resp.status_code == 412, resp.text
    assert "no tct held" in resp.text.lower()


def test_delegate_without_a_held_voucher_is_412() -> None:
    app, _agent = _app()
    resp = _client(app).post(
        "/admin/delegate",
        json={
            "held_tct_peer_port": 9999,
            "delegatee_manifest_url": "http://localhost:9998/.well-known/aitp-manifest",
            "scope": ["demo.x"],
        },
    )
    assert resp.status_code == 412, resp.text
    assert "no grant voucher held" in resp.text.lower()


def test_export_session_bundle_with_no_participants_is_412() -> None:
    app, _agent = _app()
    resp = _client(app).post("/admin/export-session-bundle", json={})
    assert resp.status_code == 412, resp.text
    assert "no participants" in resp.text.lower()


# --- 412/500: enroll-with-cp's own preconditions (distinct from Phase 5's
# transport-failure coverage in test_agent_admin_enroll.py) ---


def test_enroll_with_cp_with_no_base_url_is_412() -> None:
    app, _agent = _app(manifest_provider=lambda: '{"manifest": {}}')
    resp = _client(app).post("/admin/enroll-with-cp", json={})
    assert resp.status_code == 412, resp.text
    assert "no cp base_url" in resp.text.lower()


def test_enroll_with_cp_with_no_manifest_provider_wired_is_500() -> None:
    """The worker's own main.py forgot to pass `manifest_provider` — a
    wiring bug in THIS repo, not a caller or peer failure. D-10's taxonomy
    is exactly why this is 500 and the base-url case above is 412."""
    app, _agent = _app(manifest_provider=None)
    resp = _client(app).post(
        "/admin/enroll-with-cp", json={"cp_base_url": "http://cp.test"},
    )
    assert resp.status_code == 500, resp.text
    assert "manifest_provider" in resp.text


# --- 404: an unregistered capability ---


def test_self_execute_unknown_capability_is_404() -> None:
    app, _agent = _app(capabilities={"demo.known": lambda payload: {"ok": True}})
    resp = _client(app).post(
        "/admin/self-execute", json={"capability": "demo.unknown"},
    )
    assert resp.status_code == 404, resp.text
    assert "demo.unknown" in resp.text
    assert "not registered" in resp.text.lower()


# --- verify-session-bundle: forged bundles and a missing coordinator ---


def _participant_handshake(participant, coordinator, coord_manifest) -> str:
    sess = participant.new_session(pinned_keys=[coordinator.aid])
    rsess = coordinator.new_responder(pinned_keys=[participant.aid])
    hello = sess.build_hello(coord_manifest, ["session.member"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    return json.loads(sess.complete(commit_ack))["tct"]


def _bundle_parties():
    coord = aitp.AitpAgent.generate()
    participant = aitp.AitpAgent.generate()
    coord_manifest = coord.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    participant.build_manifest("p", "http://p/aitp/handshake/hello", ["x"])
    tct = _participant_handshake(participant, coord, coord_manifest)
    builder = aitp.SessionBundleBuilder(coord)
    builder.session_id(str(uuid.uuid4()))
    builder.issued_at(int(time.time()))
    builder.participant(participant.aid, tct)
    return coord, coord_manifest, participant, builder.build()


def _serve_manifest(monkeypatch, manifest_json: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=manifest_json)

    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(
            AsyncClient=lambda *_a, **_k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
            ),
            HTTPError=httpx.HTTPError,
        ),
    )


def _verifier_app(agent) -> FastAPI:
    app = FastAPI()
    app.include_router(build_admin_router(
        agent=agent, bootstrap={"run_id": "r", "agent_id": "p", "aitp": {}},
        held_tcts={}, revocation=RevocationState(),
    ))
    return app


def test_verify_session_bundle_with_a_tampered_signature_is_403(monkeypatch) -> None:
    _coord, coord_manifest, participant, envelope = _bundle_parties()
    doc = json.loads(envelope)
    doc["session_bundle"]["session_id"] = str(uuid.uuid4())  # changed after signing
    _serve_manifest(monkeypatch, coord_manifest)

    resp = _client(_verifier_app(participant)).post(
        "/admin/verify-session-bundle",
        json={
            "bundle_envelope": json.dumps(doc),
            "coordinator_manifest_url": "http://coord/.well-known/aitp-manifest",
        },
    )
    assert resp.status_code == 403, resp.text


def test_verify_session_bundle_under_the_wrong_coordinator_is_403(monkeypatch) -> None:
    """A genuine bundle checked against someone else's (valid) Manifest: the
    signer is not the coordinator the caller named."""
    _coord, _coord_manifest, participant, envelope = _bundle_parties()
    impostor = aitp.AitpAgent.generate()
    _serve_manifest(monkeypatch, impostor.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    ))

    resp = _client(_verifier_app(participant)).post(
        "/admin/verify-session-bundle",
        json={
            "bundle_envelope": envelope,
            "coordinator_manifest_url": "http://coord/.well-known/aitp-manifest",
        },
    )
    assert resp.status_code == 403, resp.text


def test_verify_session_bundle_without_a_coordinator_manifest_is_400() -> None:
    _coord, _coord_manifest, participant, envelope = _bundle_parties()
    resp = _client(_verifier_app(participant)).post(
        "/admin/verify-session-bundle", json={"bundle_envelope": envelope},
    )
    assert resp.status_code == 400, resp.text
    assert "coordinator_manifest_url" in resp.text
