"""RFC-AITP-0006 §4 step 7 / RFC-AITP-0011 §6 at the redeem route.

Until aitp-sdk 0.7.0 these MUSTs were unreachable: both delegation verifiers
built their context with `VerifyDelegationContext::new`, which hardcodes the
revocation hooks to `None`, and neither binding exposed a parameter. So
`/aitp/delegation/redeem` consulted **no** revocation source — not the CP
snapshot, and not even the agent's own local deny list.

That is not a bookkeeping miss. Redeeming mints a **fresh TCT** for the
delegatee, so a revoked grant kept minting credentials for a third party the
grantor never re-authorized.

This is also the one place a CP-derived entry changes a *decision* rather
than only a diagnosis. `verify_capability_tct` rejects any TCT whose `iss` is
not this agent, so on the capability path a foreign jti is refused either
way. Here the hop jtis are issued by peers, and the snapshot is the only
source the verifier has for them.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

aitp = pytest.importorskip("aitp")

from revocation_state import RevocationState  # noqa: E402


def _b64url(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _src_jti(delegation_token: str) -> str:
    """The `src_jti` of the voucher the delegation is rooted in.

    This is the handle the grantor revokes: killing the source TCT kills the
    voucher and every delegation rooted in it (RFC-AITP-0005 §8).
    """
    claims = json.loads(_b64url(delegation_token.split(".")[1]))
    return json.loads(_b64url(claims["voucher"].split(".")[1]))["src_jti"]


def _server(revocation: RevocationState):
    """Agent A — the grantor, and the only party a delegation redeems at."""
    from aitp_server import AitpServer

    agent = aitp.AitpAgent.generate()
    manifest = agent.build_manifest(
        display_name="A",
        handshake_endpoint="http://localhost:9/aitp/handshake/hello",
        offered_caps=["demo.write"],
    )
    server = AitpServer(
        agent=agent,
        manifest_json=manifest,
        port=9,
        bootstrap={
            "run_id": "r",
            "agent_id": "a",
            "aitp": {
                "seed_hex": "33" * 32,
                "display_name": "A",
                "handshake_endpoint": "http://localhost:9/aitp/handshake/hello",
                "offered_caps": ["demo.write"],
            },
        },
        revocation=revocation,
    )
    # No CP configured, so Axis B is not in play here: these tests are about
    # the deny-set itself, not about freshness policy.
    server.can_verify_revocation = False
    return server, manifest


def _delegation_for(server, manifest_json: str):
    """B handshakes with A, then delegates to C. Returns the token."""
    b = aitp.AitpAgent.generate()
    c = aitp.AitpAgent.generate()
    # Both need a manifest before they can hold a session or be delegated to.
    for agent, name, port in ((b, "B", 10), (c, "C", 11)):
        agent.build_manifest(
            display_name=name,
            handshake_endpoint=f"http://localhost:{port}/aitp/handshake/hello",
            offered_caps=["demo.write"],
        )

    sess = b.new_session()
    rsess = server.agent.new_responder()
    hello = sess.build_hello(manifest_json, ["demo.write"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    completed = json.loads(sess.complete(commit_ack))

    return b.build_delegation(completed["grant_voucher"], c.aid, ["demo.write"])


def _client(server) -> TestClient:
    app = FastAPI()
    app.include_router(server.router)
    return TestClient(app)


def _redeem(client, token):
    return client.post("/aitp/delegation/redeem", json={"delegation_token": token})


def test_a_delegation_redeems_while_nothing_is_revoked() -> None:
    """The control. Without it, a blanket-reject bug passes every test below."""
    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)

    resp = _redeem(_client(server), token)
    assert resp.status_code == 200, resp.text
    assert "tct" in resp.json()


def test_revoking_the_source_tct_refuses_to_mint_a_new_tct() -> None:
    """The MUST that was unreachable, stated in what it actually prevents."""
    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)
    client = _client(server)

    assert _redeem(client, token).status_code == 200

    state.revoke_local(_src_jti(token))
    resp = _redeem(client, token)

    assert resp.status_code == 403, (
        "a delegation rooted in a revoked source TCT still minted a fresh "
        "TCT for the delegatee"
    )
    assert "revoked" in resp.text.lower()


def test_a_cp_snapshot_entry_is_enough_to_refuse() -> None:
    """The federation case: the grantor never revoked this locally.

    On the capability path a CP-derived entry can only change the wording of
    a 403 the issuer check would have produced anyway. Here it is load-bearing
    on its own.
    """
    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)
    client = _client(server)

    now = int(time.time())
    state.apply_snapshot(
        [_src_jti(token)], published_at=now, expires_at=now + 3600
    )
    assert not state.is_locally_revoked(_src_jti(token))

    assert _redeem(client, token).status_code == 403


def test_an_unrelated_revoked_jti_does_not_block_redemption() -> None:
    """The deny-set is consulted by jti, not by "is it non-empty"."""
    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)

    state.revoke_local("00000000-0000-4000-8000-000000000000")

    assert _redeem(_client(server), token).status_code == 200


def test_a_revoked_redemption_is_reported_as_a_rejection() -> None:
    """Operators find this in telemetry, not by reading a status code."""
    import aitp_server

    events: list[tuple[str, dict]] = []

    async def _capture(event_type, _bootstrap, **fields):
        events.append((event_type, fields))

    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)
    state.revoke_local(_src_jti(token))

    original = aitp_server.emit_event
    aitp_server.emit_event = _capture
    try:
        _redeem(_client(server), token)
    finally:
        aitp_server.emit_event = original

    rejected = [f for name, f in events if name == "delegation.rejected"]
    assert rejected, f"no delegation.rejected event; saw {[n for n, _ in events]}"
    assert "revoked" in rejected[0]["error"].lower()
