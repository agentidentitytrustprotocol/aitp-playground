"""Axis B: what happens when there is no *fresh* verified snapshot.

Strictly separate from Axis A. An unverifiable snapshot is discarded
unconditionally (RFC-AITP-0008 §1.5, a MUST); these settings govern only the
absence of a fresh one. The separation is the substance of Decision D1, and
`test_soft_fail_never_rescues_a_forged_snapshot` is the assertion that keeps
the two from quietly collapsing into one switch — the collapse that makes
`aitp_verifier`'s `soft_fail` report a *forged* snapshot as not-revoked.

The clock is driven by back-dating a snapshot's `published_at`, never by
sleeping: a test that sleeps for a staleness budget is a test nobody runs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

from revocation_state import RevocationState  # noqa: E402

_MAX_STALENESS = 300


def _server(
    *,
    cp: bool = True,
    fail_mode: str = "fail_closed",
    revocation=None,
    can_verify: bool | None = None,
):
    """A server with the Axis B policy wired.

    `can_verify` is set explicitly rather than inferred, so these tests
    exercise the *policy* and not the capabilities of whichever aitp-sdk
    happens to be installed. Whether the wheel can check a signature is
    Axis A's concern and is tested in test_revocation_verify_or_discard.py.
    """
    from aitp_server import AitpServer

    agent = aitp.AitpAgent.generate()
    cp_block = (
        {
            "base_url": "http://cp.test:4000",
            "aid": "aid:pubkey:" + "A" * 43,
            "fail_mode": fail_mode,
            "max_staleness_secs": _MAX_STALENESS,
            "poll_secs": 60,
        }
        if cp
        else {}
    )
    bootstrap = {
        "run_id": "r",
        "agent_id": "a",
        "aitp": {
            "seed_hex": "22" * 32,
            "display_name": "a",
            "handshake_endpoint": "http://localhost:9/aitp/handshake/hello",
            "offered_caps": ["demo.x"],
        },
    }
    if cp_block:
        bootstrap["cp"] = cp_block
    server = AitpServer(
        agent=agent,
        manifest_json=agent.build_manifest(
            display_name="a",
            handshake_endpoint="http://localhost:9/aitp/handshake/hello",
            offered_caps=["demo.x"],
        ),
        port=9,
        bootstrap=bootstrap,
        revocation=revocation if revocation is not None else RevocationState(),
    )
    server.can_verify_revocation = cp if can_verify is None else can_verify
    return server


def _apply_snapshot(state: RevocationState, entries=(), *, age_secs: int = 0, ttl: int = 3600):
    """Put a verified snapshot in force, back-dated by `age_secs`."""
    published = int(time.time()) - age_secs
    state.apply_snapshot(entries, published_at=published, expires_at=published + ttl)


# ── criterion 5: an outage inside the budget changes nothing ─────────────


def test_a_fresh_snapshot_serves_normally() -> None:
    state = RevocationState()
    _apply_snapshot(state, [])
    server = _server(revocation=state)
    # Does not raise.
    server._enforce_revocation_freshness()


def test_an_outage_inside_the_staleness_budget_behaves_exactly_as_before() -> None:
    """The last verified snapshot legitimately bridges a short outage.

    §3.2 applies the mode only when the cached list is older than
    `max_staleness_secs` AND the endpoint is unreachable. A CP that blips for a
    minute must not turn every capability call into a 403 — otherwise
    `fail_closed` converts routine restarts into scenario failures, and someone
    disables it.
    """
    state = RevocationState()
    _apply_snapshot(state, [], age_secs=_MAX_STALENESS - 60)
    server = _server(revocation=state)
    server._enforce_revocation_freshness()  # still inside the budget


# ── criterion 6: beyond the budget, the mode decides ─────────────────────


def test_beyond_the_budget_fail_closed_rejects_with_a_degraded_state_detail() -> None:
    """And the detail must not read like a deny-list hit.

    Rendering "the CP restarted" and "this token is revoked" as the same 403 is
    exactly the confusion the Observability requirement exists to prevent.
    """
    state = RevocationState()
    _apply_snapshot(state, [], age_secs=_MAX_STALENESS + 60)
    server = _server(revocation=state, fail_mode="fail_closed")

    with pytest.raises(HTTPException) as exc:
        server._enforce_revocation_freshness()

    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert "degraded" in detail
    assert "staleness budget" in detail
    assert "revoked" not in detail, (
        "the degraded-state 403 reads like a deny-list hit; an operator "
        "cannot tell a CP outage from an actual revocation"
    )


def test_beyond_the_budget_soft_fail_serves_on_the_last_verified_deny_set() -> None:
    """Explicit opt-in, and never silent — §3.1 requires the state be logged."""
    state = RevocationState()
    _apply_snapshot(state, [], age_secs=_MAX_STALENESS + 60)
    server = _server(revocation=state, fail_mode="soft_fail")

    server._enforce_revocation_freshness()  # does not raise
    assert server._degraded_serves == 1, "a degraded serve went unrecorded"


def test_never_having_fetched_a_snapshot_is_degraded_not_current() -> None:
    """An agent that has never heard from its CP is not "nothing revoked".

    This is the state every agent starts in, so getting it wrong would make
    fail_closed a no-op until the first poll — precisely the window an attacker
    would want.
    """
    server = _server(revocation=RevocationState(), fail_mode="fail_closed")
    with pytest.raises(HTTPException) as exc:
        server._enforce_revocation_freshness()
    assert "no verified snapshot" in exc.value.detail


def test_an_expired_snapshot_is_degraded_even_inside_the_staleness_budget() -> None:
    """`expires_at` is the issuer's own deadline and outranks our budget."""
    state = RevocationState()
    _apply_snapshot(state, [], age_secs=10, ttl=5)  # young, but already expired
    server = _server(revocation=state, fail_mode="fail_closed")
    with pytest.raises(HTTPException) as exc:
        server._enforce_revocation_freshness()
    assert "expires_at" in exc.value.detail


# ── criterion 7: soft_fail is Axis B only ────────────────────────────────


@pytest.mark.skipif(
    not hasattr(aitp, "verify_revocation_list"),
    reason=(
        "needs aitp-sdk >=0.6.0 to mint the rejection; without it the forgery "
        "cannot be presented to a real ingest path and this criterion is "
        "UNTESTED, not passing."
    ),
)
def test_soft_fail_never_rescues_a_forged_snapshot() -> None:
    """The assertion that keeps the two axes apart — with a real forgery.

    Under a collapsed single switch, `soft_fail` means "could not establish
    revocation, proceed", so an attacker serving garbage gets the same outcome
    as one who suppresses the list and the whole verification effort becomes a
    no-op under attack. Here `soft_fail` must not reach authenticity at all.

    An earlier version of this test asserted that a jti it never added was
    absent — vacuously true, and it would have passed even if `soft_fail` DID
    rescue forgeries. This one presents an actual forged snapshot to the real
    ingest decision.
    """
    import base64
    import hashlib
    import json
    import uuid

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tests.unit._jcs_reference import canonicalize

    # A snapshot correctly signed by an attacker's own key — internally
    # perfect, just not from the issuer we pinned.
    attacker = Ed25519PrivateKey.generate()
    raw = attacker.public_key().public_bytes_raw()
    attacker_aid = "aid:pubkey:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    forged_jti = str(uuid.uuid4())
    now = int(time.time())
    body = {
        "version": "aitp/0.2",
        "issuer": attacker_aid,
        "published_at": now,
        "expires_at": now + 3600,
        "entries": [{"jti": forged_jti, "revoked_at": now}],
    }
    sig = attacker.sign(hashlib.sha256(canonicalize(body)).digest())
    forged = json.dumps(
        {
            "revocation_list": body,
            "signature": base64.urlsafe_b64encode(sig).decode().rstrip("="),
        }
    )

    pinned_aid = "aid:pubkey:" + "A" * 43
    state = RevocationState()
    _apply_snapshot(state, [], age_secs=_MAX_STALENESS + 60)
    server = _server(revocation=state, fail_mode="soft_fail")

    # Axis A runs regardless of fail_mode: the forgery is rejected at ingest.
    with pytest.raises(Exception):
        aitp.verify_revocation_list(forged, pinned_aid)

    # And soft_fail — which governs only the ABSENCE of a fresh snapshot —
    # cannot put it into force.
    server._enforce_revocation_freshness()  # proceeds, because soft_fail
    assert forged_jti not in state.effective_jtis, (
        "a forged snapshot's entries reached the deny-set under soft_fail — "
        "the two axes have collapsed into one switch"
    )
    assert state.snapshot is not None and state.snapshot_entry_count == 0, (
        "the previously verified snapshot was replaced by an unverified one"
    )


def test_the_can_verify_inference_is_exercised_not_just_overridden() -> None:
    """Cover the constructor's own inference, not the test helper's override.

    `_server()` sets `can_verify_revocation` explicitly so the policy tests do
    not depend on the installed wheel — which means a bug in the real
    inference (wrong bootstrap key, inverted condition) would pass every other
    test in this file.
    """
    from aitp_server import AitpServer

    def _build(cp_block):
        agent = aitp.AitpAgent.generate()
        bs = {
            "run_id": "r",
            "agent_id": "a",
            "aitp": {
                "seed_hex": "33" * 32,
                "display_name": "a",
                "handshake_endpoint": "http://localhost:9/x",
                "offered_caps": ["demo.x"],
            },
        }
        if cp_block:
            bs["cp"] = cp_block
        return AitpServer(
            agent=agent,
            manifest_json=agent.build_manifest(
                display_name="a",
                handshake_endpoint="http://localhost:9/x",
                offered_caps=["demo.x"],
            ),
            port=9,
            bootstrap=bs,
        )

    assert _build({}).can_verify_revocation is False, "no CP"
    assert _build({"base_url": "http://cp"}).can_verify_revocation is False, "no pin"
    assert (
        _build({"base_url": "http://cp", "aid": "aid:pubkey:" + "A" * 43}).can_verify_revocation
        is True
    ), "CP + pin should enable verification"
    # The SDK's capability is deliberately NOT part of this: an old wheel must
    # leave a pinned deployment DEGRADED, never silently unchecked.


# ── criterion 8: local revocations enforce in every state ────────────────


@pytest.mark.parametrize(
    "fail_mode,age",
    [
        ("fail_closed", 0),
        ("fail_closed", _MAX_STALENESS + 60),
        ("soft_fail", 0),
        ("soft_fail", _MAX_STALENESS + 60),
    ],
)
def test_local_revocations_enforce_in_every_posture(fail_mode: str, age: int) -> None:
    state = RevocationState()
    state.revoke_local("mine")
    _apply_snapshot(state, [], age_secs=age)
    server = _server(revocation=state, fail_mode=fail_mode)

    assert server.revocation.is_revoked("mine")
    assert server.revocation.is_locally_revoked("mine")


def test_a_cp_without_a_pinned_aid_is_unchecked_not_degraded() -> None:
    """The regression a federated e2e test caught, and the reason it matters.

    An agent with a CP URL but no pinned issuer cannot verify anything — so it
    is *unconfigured*, not *degraded*. Treating it as degraded made
    `fail_closed` reject every capability call on any deployment that had not
    yet set the new `CP_AID` variable, which is all of them at the moment the
    feature ships. Secure-by-default has to survive the upgrade that
    introduces it, or the first person to hit it turns the mode off.
    """
    state = RevocationState()
    state.revoke_local("mine")
    server = _server(revocation=state, fail_mode="fail_closed", can_verify=False)

    server._enforce_revocation_freshness()  # must NOT raise
    assert server.revocation.is_revoked("mine"), (
        "local revocations must still enforce when verification is unconfigured"
    )


def test_with_no_cp_the_posture_is_unchecked_and_never_degraded() -> None:
    """No control plane is a *named* posture, not a degraded one.

    Rejecting every call because an agent was never given a CP would make the
    local-only scenarios unrunnable. aitp-rs takes the same line: the unsafe
    direction has to be named (`accept_unchecked_revocation_dangerous`), not
    stumbled into — so it is logged once at start-up.
    """
    state = RevocationState()
    state.revoke_local("mine")
    server = _server(cp=False, revocation=state)

    assert server.cp_configured is False
    server._enforce_revocation_freshness()  # does not raise
    assert server.revocation.is_revoked("mine"), (
        "local revocations must still enforce without a control plane"
    )


def test_a_revoked_jti_reports_as_revoked_even_while_degraded() -> None:
    """Ordering: the deny-set check must precede the freshness check.

    Both produce a 403, so swapping them looks harmless — and would be
    invisible to every other test here. It is not harmless: an operator who
    revoked a token and then sees "revocation state degraded" goes hunting a
    control-plane outage that has nothing to do with why the call failed.
    """
    state = RevocationState()
    state.revoke_local("jti-abc")
    _apply_snapshot(state, [], age_secs=_MAX_STALENESS + 60)  # also degraded
    server = _server(revocation=state, fail_mode="fail_closed")

    # Both conditions hold at once; the deny-set must win the explanation.
    assert server.revocation.is_revoked("jti-abc")
    with pytest.raises(HTTPException) as exc:
        server._enforce_revocation_freshness()
    assert "degraded" in exc.value.detail  # freshness alone says degraded

    # Now through the real path, where ordering decides.
    import tct_claims

    monkey = getattr(tct_claims, "decode_claims")
    try:
        tct_claims.decode_claims = lambda _t: {"jti": "jti-abc", "iss": server.agent.aid}
        import aitp_server as mod

        mod.decode_claims = tct_claims.decode_claims
        with pytest.raises(HTTPException) as exc2:
            server.verify_capability_tct("dummy.token.value", "demo.x")
    finally:
        tct_claims.decode_claims = monkey
        import aitp_server as mod

        mod.decode_claims = monkey

    assert "revoked" in exc2.value.detail, (
        f"a revoked jti reported as {exc2.value.detail!r} — the freshness "
        "check is running before the deny-set check"
    )
    assert "degraded" not in exc2.value.detail
