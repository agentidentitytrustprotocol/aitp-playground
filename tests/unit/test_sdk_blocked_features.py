"""Smoke tests for the formerly-SDK-blocked surfaces.

These verify that the playground side compiles + exercises the SDK
methods correctly without actually spawning agent subprocesses. The
full e2e behavior is covered by docker-compose integration tests.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

import aitp


# ── P-256 suite ─────────────────────────────────────────────────────────────


def test_p256_agent_generates_p256_aid() -> None:
    """P-256 suite knob produces an aid:pubkey:p256:<44> AID."""
    a = aitp.AitpAgent.generate("p256")
    assert a.aid.startswith("aid:pubkey:p256:")


def test_p256_from_seed_is_deterministic() -> None:
    seed = bytes(range(32))
    a = aitp.AitpAgent.from_seed(seed, "p256")
    b = aitp.AitpAgent.from_seed(seed, "p256")
    assert a.aid == b.aid


def test_unknown_signing_suite_rejected() -> None:
    with pytest.raises(ValueError, match="unknown suite"):
        aitp.AitpAgent.generate("rsa")


# Note: a full P-256 handshake requires OIDC identity because the v0.1
# manifest's pinned_key identity_hint embeds Ed25519-only public_key
# bytes (see aitp-py/tests/test_p256_suite.py:8-11). Cross-suite OIDC
# coverage lives in test_oidc_handshake_p256_initiator below.


# ── TCT renewal ─────────────────────────────────────────────────────────────


def test_tct_renewal_round_trip() -> None:
    """Build a TCT via a full handshake, renew it, and confirm the new
    envelope has a different jti but identical subject + grants."""
    holder = aitp.AitpAgent.generate()
    issuer = aitp.AitpAgent.generate()
    h_m = holder.build_manifest("h", "http://h/aitp/handshake/hello", ["demo.x"])
    i_m = issuer.build_manifest("i", "http://i/aitp/handshake/hello", ["demo.y"])
    sh = holder.new_session()
    si = issuer.new_responder()
    hello = sh.build_hello(i_m, ["demo.y"])
    ack, sid = si.process_hello(hello)
    commit = sh.process_hello_ack(ack, sid)
    commit_ack, _ = si.process_commit(commit)
    held = sh.complete(commit_ack)
    orig = json.loads(held)["tct"]

    request_payload = holder.build_renewal_request(held)
    manifest_exp = int(time.time()) + 3600
    renewed = issuer.process_renewal_request(request_payload, manifest_exp, 3600)
    new_tct = json.loads(renewed)["tct"]

    assert new_tct["jti"] != orig["jti"]
    assert new_tct["subject"] == orig["subject"]
    assert sorted(new_tct["grants"]) == sorted(orig["grants"])
    # Within the same wall-clock second the renewed expiry may equal the
    # original — the load-bearing assertion is the fresh jti.
    assert new_tct["expires_at"] >= orig["expires_at"]


# ── Session bundle ──────────────────────────────────────────────────────────


def _participant_handshake(participant, coordinator, coord_manifest):
    """Participant initiates a handshake against the coordinator (responder).
    Returns the coordinator-issued TCT the participant now holds."""
    sess = participant.new_session()
    rsess = coordinator.new_responder()
    hello = sess.build_hello(coord_manifest, ["session.member"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    return sess.complete(commit_ack)


def test_session_bundle_export_and_verify() -> None:
    """RFC-0010 bundle round-trip: coordinator is the issuer
    (responder), participants are initiators. The coordinator-issued
    TCTs get packaged into a SessionBundleEnvelope; a participant
    verifies it and is named in active_aids."""
    coord = aitp.AitpAgent.generate()
    a = aitp.AitpAgent.generate()
    b = aitp.AitpAgent.generate()
    coord_m = coord.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    a.build_manifest("a", "http://a/aitp/handshake/hello", ["x"])
    b.build_manifest("b", "http://b/aitp/handshake/hello", ["x"])

    a_tct = _participant_handshake(a, coord, coord_m)
    b_tct = _participant_handshake(b, coord, coord_m)

    builder = aitp.SessionBundleBuilder(coord)
    builder.session_id("12345678-1234-1234-1234-1234567890ab")
    builder.issued_at(int(time.time()))
    builder.participant(a.aid, a_tct)
    builder.participant(b.aid, b_tct)
    envelope = builder.build()

    outcome = aitp.verify_session_bundle(envelope, a.aid)
    assert outcome["kind"] == "clear"
    assert a.aid in outcome["active_aids"]
    assert b.aid in outcome["active_aids"]


# ── SPKI pinning ────────────────────────────────────────────────────────────


_TEST_CERT_DER_B64 = (
    "MIHqMIGdoAMCAQICAQEwBQYDK2VwMB8xHTAbBgNVBAMMFGFpdHAtcGxheWdyb3VuZC10ZXN0"
    "MB4XDTI1MDEwMTAwMDAwMFoXDTM1MDEwMTAwMDAwMFowHzEdMBsGA1UEAwwUYWl0cC1wbGF5"
    "Z3JvdW5kLXRlc3QwKjAFBgMrZXADIQADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUx"
    "uDAFBgMrZXADQQBf7eAcMWnzNUS7/K6nk22d1fJX7vE/2e0EnW6KEb7LCBrIwvavlKomxu5o"
    "NStataOrBnnsS4PgKTMU/ItJlPUP"
)
_TEST_CERT_SPKI_B64 = "oFCDfYUHBYLM9zlLCYiEfMMSy4glm4lImfbyOc8XkaU="


def test_compute_spki_hash_is_deterministic() -> None:
    der = base64.b64decode(_TEST_CERT_DER_B64)
    computed = bytes(aitp.compute_spki_hash(der))
    assert base64.b64encode(computed).decode() == _TEST_CERT_SPKI_B64


def test_spki_pin_verifier_matches_and_rejects() -> None:
    der = base64.b64decode(_TEST_CERT_DER_B64)
    matching_pin = base64.b64decode(_TEST_CERT_SPKI_B64)
    other_pin = bytes(32)
    v_match = aitp.SpkiPinVerifier([matching_pin])
    v_miss = aitp.SpkiPinVerifier([other_pin])
    assert v_match.is_pinned(der) is True
    assert v_miss.is_pinned(der) is False


# ── OIDC mock issuer ────────────────────────────────────────────────────────


def test_oidc_run_issuer_produces_valid_jwk() -> None:
    """The playground's per-run issuer should produce a JWK that the
    SDK's JwksProvider accepts without complaint."""
    from aitp_playground.trust.oidc_issuer import RunOidcIssuer
    issuer = RunOidcIssuer.generate()
    provider = aitp.JwksProvider({issuer.issuer_url: [issuer.public_jwk]})
    assert issuer.issuer_url in provider.issuers()


def test_oidc_handshake_p256_initiator() -> None:
    """Cross-suite OIDC handshake: P-256 initiator + Ed25519 responder.
    Mirrors what the oidc-identity scenario's p256-suite template
    exercises end-to-end."""
    from aitp_playground.trust.oidc_issuer import RunOidcIssuer, mint_jwt
    issuer = RunOidcIssuer.generate()
    a = aitp.AitpAgent.generate("p256")  # P-256 + OIDC
    b = aitp.AitpAgent.generate()        # Ed25519 + pinned-key
    assert a.aid.startswith("aid:pubkey:p256:")
    a_m = a.build_manifest(
        "a", "http://a/aitp/handshake/hello", ["demo.x"],
        identity_type="oidc",
        oidc_issuer=issuer.issuer_url,
        oidc_subject="alice",
    )
    b_m = b.build_manifest("b", "http://b/aitp/handshake/hello", ["demo.y"])
    provider = aitp.JwksProvider({issuer.issuer_url: [issuer.public_jwk]})
    a_jkt = aitp.compute_aid_jkt(a.aid)
    now = int(time.time())
    a_mint = lambda nonce: mint_jwt(
        private_seed_b64=issuer.private_seed_b64,
        kid=issuer.kid,
        issuer_url=issuer.issuer_url,
        subject="alice",
        audience=b.aid,
        nonce=nonce,
        cnf_jkt=a_jkt,
        now_unix_secs=now,
    )
    sa = a.new_session(jwks=provider, trust_anchors=[issuer.issuer_url])
    sb = b.new_responder(jwks=provider, trust_anchors=[issuer.issuer_url])
    hello = sa.build_hello(b_m, ["demo.y"], oidc_mint_jwt=a_mint)
    ack, sid = sb.process_hello(hello)
    commit = sa.process_hello_ack(ack, sid)
    commit_ack, b_held = sb.process_commit(commit)
    a_held = sa.complete(commit_ack)
    assert a.verify_tct(a_held, "demo.y").peer_aid == b.aid
    assert b.verify_tct(b_held, "demo.x").peer_aid == a.aid


def test_oidc_handshake_initiator_oidc_responder_pinned() -> None:
    """One agent identifies via OIDC, peer is pinned-key; the SDK
    completes the handshake when JwksProvider + trust_anchors are
    threaded into both sides. Mirrors the playground's OIDC scenario."""
    from aitp_playground.trust.oidc_issuer import RunOidcIssuer, mint_jwt
    issuer = RunOidcIssuer.generate()

    a = aitp.AitpAgent.generate()  # OIDC
    b = aitp.AitpAgent.generate()  # pinned-key
    a_m = a.build_manifest(
        "a", "http://a/aitp/handshake/hello", ["demo.x"],
        identity_type="oidc",
        oidc_issuer=issuer.issuer_url,
        oidc_subject="alice",
    )
    b_m = b.build_manifest("b", "http://b/aitp/handshake/hello", ["demo.y"])
    provider = aitp.JwksProvider({issuer.issuer_url: [issuer.public_jwk]})

    a_jkt = aitp.compute_aid_jkt(a.aid)
    now = int(time.time())
    a_mint = lambda nonce: mint_jwt(
        private_seed_b64=issuer.private_seed_b64,
        kid=issuer.kid,
        issuer_url=issuer.issuer_url,
        subject="alice",
        audience=b.aid,
        nonce=nonce,
        cnf_jkt=a_jkt,
        now_unix_secs=now,
    )

    sa = a.new_session(jwks=provider, trust_anchors=[issuer.issuer_url])
    sb = b.new_responder(jwks=provider, trust_anchors=[issuer.issuer_url])
    hello = sa.build_hello(b_m, ["demo.y"], oidc_mint_jwt=a_mint)
    ack, sid = sb.process_hello(hello)
    commit = sa.process_hello_ack(ack, sid)
    commit_ack, b_held = sb.process_commit(commit)
    a_held = sa.complete(commit_ack)

    assert a.verify_tct(a_held, "demo.y").peer_aid == b.aid
    assert b.verify_tct(b_held, "demo.x").peer_aid == a.aid
