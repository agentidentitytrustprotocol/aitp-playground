"""Route-level rejection branches in `agent_admin.py` — untested before this
file: every non-2xx status this module raises directly (as opposed to one
mirrored from a peer's response) had zero coverage. Each of these guards is
the module's own precondition or wiring check, not a downstream failure —
`DECISIONS.md` D-10's taxonomy names them 412 (caller state), 404 (unknown
capability), 500 (wiring bug here).

Also covers `/admin/verify-session-bundle`'s forged-bundle behavior, which
was likewise untested.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

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


# --- verify-session-bundle: a forged bundle, currently uncaught ---


def _participant_handshake(participant, coordinator, coord_manifest) -> str:
    sess = participant.new_session()
    rsess = coordinator.new_responder()
    hello = sess.build_hello(coord_manifest, ["session.member"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    return json.loads(sess.complete(commit_ack))["tct"]


def test_verify_session_bundle_with_a_tampered_signature_is_rejected() -> None:
    """`/admin/verify-session-bundle` has no `try`/`except` around
    `aitp.verify_session_bundle` — unlike every other verify site in this
    module. A tampered bundle raises a bare `RuntimeError` today, which
    propagates as a 500 rather than the 403/502 shape every other rejection
    in this module uses. Recorded honestly: this test pins CURRENT
    behaviour (a forged bundle IS rejected — the request does not succeed —
    just not with the taxonomy the rest of the module has), not the taxonomy
    this route would ideally have. Fixing the shape is a follow-up, not a
    silent asserted improvement.
    """
    coord = aitp.AitpAgent.generate()
    participant = aitp.AitpAgent.generate()
    coord_manifest = coord.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    participant.build_manifest(
        "p", "http://p/aitp/handshake/hello", ["x"],
    )
    tct = _participant_handshake(participant, coord, coord_manifest)

    builder = aitp.SessionBundleBuilder(coord)
    builder.session_id("12345678-1234-1234-1234-1234567890ab")
    builder.issued_at(int(time.time()))
    builder.participant(participant.aid, tct)
    envelope = builder.build()

    doc = json.loads(envelope)
    doc["session_bundle"]["session_id"] = "00000000-0000-0000-0000-000000000000"
    tampered = json.dumps(doc)

    coord_bootstrap = {"run_id": "r", "agent_id": "coord", "aitp": {}}
    router = build_admin_router(
        agent=coord, bootstrap=coord_bootstrap, held_tcts={},
        revocation=RevocationState(),
    )
    app = FastAPI()
    app.include_router(router)

    resp = _client(app).post(
        "/admin/verify-session-bundle", json={"bundle_envelope": tampered},
    )
    assert resp.status_code != 200, (
        "a tampered session bundle must not be reported as verified"
    )
