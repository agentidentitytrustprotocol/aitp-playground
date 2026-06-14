"""Smoke tests for the formerly-SDK-blocked surfaces.

These verify that the playground side compiles + exercises the SDK
methods correctly without actually spawning agent subprocesses. The
full e2e behavior is covered by docker-compose integration tests.

Both ``aitp`` (the Rust-backed wheel built from sibling ``aitp-rs``)
and ``cryptography`` are needed; the module skips cleanly when CI
runs without them per the convention in `.github/workflows/test.yml`.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

aitp = pytest.importorskip(
    "aitp", reason="aitp SDK wheel not installed", exc_type=ImportError,
)
pytest.importorskip(
    "cryptography", reason="cryptography not installed", exc_type=ImportError,
)

# Surfaces that depend on Cargo features the wheel may have been built
# without — skip individual tests rather than fail collection.
_HAS_BUNDLE = hasattr(aitp, "SessionBundleBuilder")
_HAS_PINNING = hasattr(aitp, "SpkiPinVerifier")
_HAS_RENEWAL = hasattr(aitp.AitpAgent, "build_renewal_request")
_HAS_OIDC = hasattr(aitp, "JwksProvider")
_HAS_TCT_CACHE = hasattr(aitp, "TctStore")
_HAS_MULTIHOP = hasattr(aitp, "verify_delegation_experimental_multihop")


def _claims(token: str) -> dict:
    """Decode the (unverified) claims of a v0.2 compact-JWS TCT — the SDK's
    own Python test idiom for inspecting an opaque token's payload."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


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


@pytest.mark.skipif(not _HAS_RENEWAL, reason="aitp built without experimental-renewal")
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
    held_tct = json.loads(sh.complete(commit_ack))["tct"]
    orig = _claims(held_tct)

    request_payload = holder.build_renewal_request(held_tct)
    manifest_exp = int(time.time()) + 3600
    renewed = json.loads(
        issuer.process_renewal_request(request_payload, manifest_exp, 3600)
    )
    new_tct = _claims(renewed["tct"])

    assert new_tct["jti"] != orig["jti"]
    assert new_tct["sub"] == orig["sub"]
    assert sorted(new_tct["grants"]) == sorted(orig["grants"])
    # Within the same wall-clock second the renewed expiry may equal the
    # original — the load-bearing assertion is the fresh jti.
    assert new_tct["exp"] >= orig["exp"]


# ── TCT verification cache (RFC-AITP-0005 hot path) ─────────────────────────


@pytest.mark.skipif(not _HAS_TCT_CACHE, reason="aitp built without TctStore")
def test_tct_cache_hit_miss_len_delta() -> None:
    """AitpServer's hit/miss accounting relies on store.len() growing on a
    miss and staying flat on a byte-identical hit. Pin that contract against
    the real SDK so the heuristic in verify_capability_tct stays valid."""
    holder = aitp.AitpAgent.generate()
    issuer = aitp.AitpAgent.generate()
    holder.build_manifest("h", "http://h/aitp/handshake/hello", ["demo.x"])
    i_m = issuer.build_manifest("i", "http://i/aitp/handshake/hello", ["demo.y"])
    sh = holder.new_session()
    si = issuer.new_responder()
    hello = sh.build_hello(i_m, ["demo.y"])
    ack, sid = si.process_hello(hello)
    commit = sh.process_hello_ack(ack, sid)
    commit_ack, _ = si.process_commit(commit)
    held_tct = json.loads(sh.complete(commit_ack))["tct"]
    claims = _claims(held_tct)
    audience = claims.get("aud") or claims.get("sub")

    store = aitp.TctStore(256)
    assert store.len() == 0
    id1 = issuer.verify_tct_cached(held_tct, "demo.y", store, expected_audience=audience)
    assert store.len() == 1  # miss: a new entry was inserted
    id2 = issuer.verify_tct_cached(held_tct, "demo.y", store, expected_audience=audience)
    assert store.len() == 1  # hit: no new entry
    assert id1.peer_aid == id2.peer_aid
    assert id1.jti == id2.jti


# ── Session bundle ──────────────────────────────────────────────────────────


def _participant_handshake(participant, coordinator, coord_manifest):
    """Participant initiates a handshake against the coordinator (responder).
    Returns the coordinator-issued TCT (compact-JWS token) the participant
    now holds."""
    sess = participant.new_session()
    rsess = coordinator.new_responder()
    hello = sess.build_hello(coord_manifest, ["session.member"])
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    return json.loads(sess.complete(commit_ack))["tct"]


@pytest.mark.skipif(not _HAS_BUNDLE, reason="aitp built without experimental-bundle")
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


@pytest.mark.skipif(not _HAS_PINNING, reason="aitp built without experimental-pinning")
def test_compute_spki_hash_is_deterministic() -> None:
    der = base64.b64decode(_TEST_CERT_DER_B64)
    computed = bytes(aitp.compute_spki_hash(der))
    assert base64.b64encode(computed).decode() == _TEST_CERT_SPKI_B64


@pytest.mark.skipif(not _HAS_PINNING, reason="aitp built without experimental-pinning")
def test_spki_pin_verifier_matches_and_rejects() -> None:
    der = base64.b64decode(_TEST_CERT_DER_B64)
    matching_pin = base64.b64decode(_TEST_CERT_SPKI_B64)
    other_pin = bytes(32)
    v_match = aitp.SpkiPinVerifier([matching_pin])
    v_miss = aitp.SpkiPinVerifier([other_pin])
    assert v_match.is_pinned(der) is True
    assert v_miss.is_pinned(der) is False


# ── OIDC mock issuer ────────────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_OIDC, reason="aitp built without OIDC JwksProvider")
def test_oidc_run_issuer_produces_valid_jwk() -> None:
    """The playground's per-run issuer should produce a JWK that the
    SDK's JwksProvider accepts without complaint."""
    from aitp_playground.trust.oidc_issuer import RunOidcIssuer
    issuer = RunOidcIssuer.generate()
    provider = aitp.JwksProvider({issuer.issuer_url: [issuer.public_jwk]})
    assert issuer.issuer_url in provider.issuers()


@pytest.mark.skipif(not _HAS_OIDC, reason="aitp built without OIDC JwksProvider")
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
    commit_ack, b_completed = sb.process_commit(commit)
    a_held = json.loads(sa.complete(commit_ack))["tct"]
    b_held = json.loads(b_completed)["tct"]
    assert a.verify_tct(a_held, "demo.y").peer_aid == b.aid
    assert b.verify_tct(b_held, "demo.x").peer_aid == a.aid


@pytest.mark.skipif(not _HAS_OIDC, reason="aitp built without OIDC JwksProvider")
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
    commit_ack, b_completed = sb.process_commit(commit)
    a_held = json.loads(sa.complete(commit_ack))["tct"]
    b_held = json.loads(b_completed)["tct"]

    assert a.verify_tct(a_held, "demo.y").peer_aid == b.aid
    assert b.verify_tct(b_held, "demo.x").peer_aid == a.aid
