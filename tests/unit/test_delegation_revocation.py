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


# --- Phase 7: rejection branches this file's harness reaches but never tested ---


def test_an_old_sdk_that_cannot_take_the_revocation_parameter_fails_closed() -> None:
    """`aitp_server.py`'s `except TypeError` — item 1.

    The whole point of this guard is that it does not silently drop the
    deny-set on an old SDK; it refuses loudly with 503. A test asserting only
    "this is rejected" would pass on a `hasattr`-probe-and-drop
    implementation just as well — the status code and the message naming the
    SDK are both load-bearing.
    """
    import aitp_server

    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)

    def _too_old(*_args, **_kwargs):
        raise TypeError("verify_delegation() takes 2 positional arguments but 3 were given")

    original = aitp_server.aitp.verify_delegation
    aitp_server.aitp.verify_delegation = _too_old
    try:
        resp = _redeem(_client(server), token)
    finally:
        aitp_server.aitp.verify_delegation = original

    assert resp.status_code == 503, resp.text
    assert "aitp-sdk" in resp.text.lower()
    assert ">=0.7.0" in resp.text


def test_redeem_enforces_axis_b_freshness_before_minting() -> None:
    """`_enforce_revocation_freshness()` on the mint path — item 2.

    `test_delegation_revocation.py`'s own `_server()` sets
    `can_verify_revocation = False` specifically to keep Axis B out of play
    for every other test in this file (a deliberate choice, not an oversight
    — see its docstring). This test is the one exception: it turns Axis B
    back on, with no snapshot ever applied, so the default `fail_closed`
    posture is "degraded" — and confirms redeeming refuses on that basis
    alone, before delegation verification runs at all.
    """
    state = RevocationState()
    server, manifest = _server(state)
    token = _delegation_for(server, manifest)
    # Axis B back on, deliberately, unlike every other test in this file.
    server.can_verify_revocation = True

    resp = _redeem(_client(server), token)

    assert resp.status_code == 403, resp.text
    assert "degraded" in resp.text.lower()
    assert "no verified snapshot" in resp.text.lower()


def test_multihop_branch_also_enforces_the_deny_set() -> None:
    """The multi-hop verifier's deny-set argument — item 3.

    `aitp.verify_delegation_multihop` is consulted with `revoked_jtis` on
    every call once `allow_multihop_delegation` is set, whether or not the
    presented token is actually a multi-hop chain (RFC-AITP-0011 §6 makes
    the root voucher's `src_jti` a MUST-reject same as the single-hop path).
    A single-hop token is enough to prove THIS branch — not the `else` one —
    is what enforced the revocation.
    """
    import aitp_server

    assert hasattr(aitp_server.aitp, "verify_delegation_multihop"), (
        "installed aitp-sdk lacks verify_delegation_multihop — this test "
        "needs the multihop-delegation feature the 0.7.0+ floor assumes"
    )

    state = RevocationState()
    server, manifest = _server(state)
    server.bootstrap.setdefault("aitp", {})["allow_multihop_delegation"] = True
    token = _delegation_for(server, manifest)

    state.revoke_local(_src_jti(token))
    resp = _redeem(_client(server), token)

    assert resp.status_code == 403, (
        "the multihop branch did not honour the deny-set for a revoked "
        f"source jti: {resp.text}"
    )


def test_capability_tct_issuer_mismatch_is_refused_before_signature_verification() -> None:
    """`verify_capability_tct`'s issuer-AID guard — item 4.

    A TCT genuinely issued by a DIFFERENT agent (not forged — a real
    handshake, a real signature) must still be refused here: this agent only
    ever honours TCTs it issued itself. The check runs on the decoded
    `iss` claim before any cryptographic verification, so this needs no
    tampering to prove — a TCT from the wrong issuer is enough by itself.
    """
    state = RevocationState()
    server, _manifest = _server(state)  # this agent — the verifier

    # A different agent issues a TCT to some holder, via a real handshake.
    other_issuer = aitp.AitpAgent.generate()
    holder = aitp.AitpAgent.generate()
    other_manifest = other_issuer.build_manifest(
        display_name="other-issuer",
        handshake_endpoint="http://localhost:12/aitp/handshake/hello",
        offered_caps=["demo.write"],
    )
    holder.build_manifest(
        display_name="holder",
        handshake_endpoint="http://localhost:13/aitp/handshake/hello",
        offered_caps=["demo.write"],
    )
    sess = holder.new_session()
    rsess = other_issuer.new_responder()
    hello = sess.build_hello(other_manifest, ["demo.write"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    foreign_tct = json.loads(sess.complete(commit_ack))["tct"]

    with pytest.raises(Exception) as excinfo:
        server.verify_capability_tct(foreign_tct, "demo.write")

    detail = str(getattr(excinfo.value, "detail", excinfo.value))
    assert "issuer mismatch" in detail.lower()
    assert other_issuer.aid in detail
