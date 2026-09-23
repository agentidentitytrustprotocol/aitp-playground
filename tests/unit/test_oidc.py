"""Unit tests for the agent-side OIDC helpers (agents/base/oidc.py).

`OidcContext.mint_jwt_for` and `peer_aid_from_hello_envelope` back every OIDC
handshake this playground runs, but neither was covered outside a full
subprocess e2e scenario. `tests/unit/test_sdk_blocked_features.py` already
proves the underlying SDK primitives (`JwksProvider`, `compute_aid_jkt`,
`verify_oidc`) work, using its own hand-rolled `mint_jwt` copy from
`aitp_playground.trust.oidc_issuer` — the *test-side* mock issuer. This file
tests the actual *production* code path instead: the `OidcContext` class an
agent worker really constructs from its bootstrap and really calls during a
handshake.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

from oidc import OidcContext, peer_aid_from_hello_envelope  # noqa: E402

from aitp_playground.trust.oidc_issuer import RunOidcIssuer  # noqa: E402


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _oidc_bootstrap(issuer: RunOidcIssuer, *, identity_type: str = "oidc", subject: str = "alice") -> dict:
    return {
        "oidc": {
            "issuer_url": issuer.issuer_url,
            "kid": issuer.kid,
            "public_jwk": issuer.public_jwk,
            "private_seed_b64": issuer.private_seed_b64,
        },
        "aitp": {"identity_type": identity_type, "oidc_subject": subject},
    }


# ── OidcContext construction ──────────────────────────────────────────────


def test_context_is_disabled_with_no_oidc_block() -> None:
    ctx = OidcContext({"aitp": {}})
    assert ctx.enabled is False
    assert ctx.jwks is None
    assert ctx.trust_anchors is None
    assert ctx.identity_type == "pinned_key"


def test_context_builds_a_jwks_provider_for_any_agent_with_an_oidc_block() -> None:
    """Even a pinned-key agent gets the verifier provider, so it can accept
    an OIDC peer — the whole point of the module docstring's "Every agent —
    even pinned-key ones — gets the verifier provider" note."""
    issuer = RunOidcIssuer.generate()
    ctx = OidcContext(_oidc_bootstrap(issuer, identity_type="pinned_key"))
    assert ctx.enabled is True
    assert ctx.identity_type == "pinned_key"
    assert isinstance(ctx.jwks, aitp.JwksProvider)
    assert ctx.trust_anchors == [issuer.issuer_url]


# ── mint_jwt_for ──────────────────────────────────────────────────────────


def test_mint_jwt_for_is_none_when_this_agent_is_not_oidc_typed() -> None:
    """A pinned-key agent never needs to mint an OIDC token for itself —
    the SDK only calls the callback when the LOCAL manifest declares OIDC
    identity."""
    issuer = RunOidcIssuer.generate()
    ctx = OidcContext(_oidc_bootstrap(issuer, identity_type="pinned_key"))
    agent = aitp.AitpAgent.generate()
    assert ctx.mint_jwt_for(audience="aid:pubkey:someone", agent=agent) is None


def test_mint_jwt_for_is_none_without_a_private_seed() -> None:
    """oidc.identity_type == "oidc" but no private_seed_b64 in the bootstrap
    — e.g. a peer that merely accepts OIDC identities but isn't the subject
    itself. Must not raise; must not mint."""
    issuer = RunOidcIssuer.generate()
    bs = _oidc_bootstrap(issuer)
    del bs["oidc"]["private_seed_b64"]
    ctx = OidcContext(bs)
    agent = aitp.AitpAgent.generate()
    assert ctx.mint_jwt_for(audience="aid:pubkey:someone", agent=agent) is None


def test_mint_jwt_for_binds_cnf_jkt_to_the_agents_own_aid() -> None:
    issuer = RunOidcIssuer.generate()
    ctx = OidcContext(_oidc_bootstrap(issuer, subject="alice"))
    agent = aitp.AitpAgent.generate()

    mint = ctx.mint_jwt_for(audience="aid:pubkey:bob", agent=agent, ttl_secs=120)
    assert mint is not None
    token = mint("nonce-abc123")

    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(_b64u_decode(header_b64))
    claims = json.loads(_b64u_decode(payload_b64))

    assert header == {"alg": "EdDSA", "typ": "JWT", "kid": issuer.kid}
    assert claims["iss"] == issuer.issuer_url
    assert claims["sub"] == "alice"
    assert claims["aud"] == "aid:pubkey:bob"
    assert claims["nonce"] == "nonce-abc123"
    assert claims["exp"] - claims["iat"] == 120
    assert claims["cnf"] == {"jkt": aitp.compute_aid_jkt(agent.aid)}, (
        "the cnf.jkt binding must be the MINTING agent's own AID thumbprint, "
        "not the audience's — this is what lets a verifier tie the JWT to "
        "the holder's AITP key"
    )


def test_mint_jwt_for_produces_a_jwt_a_real_handshake_actually_accepts() -> None:
    """The end-to-end proof: plug the production `mint_jwt_for` callback into
    a real `build_hello` and complete a real handshake against an SDK
    responder configured with the matching JwksProvider. If the cnf.jkt
    binding or claim shape were wrong, this — not just a claims-shape
    assertion — is what would fail."""
    issuer = RunOidcIssuer.generate()
    a = aitp.AitpAgent.generate()  # OIDC-identified initiator
    b = aitp.AitpAgent.generate()  # pinned-key responder that accepts OIDC peers

    a.build_manifest(
        "a", "http://a/aitp/handshake/hello", ["demo.x"],
        identity_type="oidc", oidc_issuer=issuer.issuer_url, oidc_subject="alice",
    )
    b_manifest = b.build_manifest("b", "http://b/aitp/handshake/hello", ["demo.y"])

    ctx = OidcContext(_oidc_bootstrap(issuer, subject="alice"))
    mint_cb = ctx.mint_jwt_for(audience=b.aid, agent=a)
    assert mint_cb is not None

    sess = a.new_session(jwks=ctx.jwks, trust_anchors=ctx.trust_anchors)
    responder = b.new_responder(jwks=ctx.jwks, trust_anchors=ctx.trust_anchors)

    hello = sess.build_hello(b_manifest, ["demo.y"], oidc_mint_jwt=mint_cb)
    ack, session_id = responder.process_hello(hello)
    commit = sess.process_hello_ack(ack, session_id)
    commit_ack, _completed_b = responder.process_commit(commit)
    a_held = json.loads(sess.complete(commit_ack))["tct"]

    assert a.verify_tct(a_held, "demo.y").peer_aid == b.aid, (
        "a handshake authenticated with mint_jwt_for's JWT did not yield a "
        "usable TCT — the cnf.jkt binding or claim shape must be wrong"
    )


# ── peer_aid_from_hello_envelope ─────────────────────────────────────────


def test_peer_aid_from_hello_envelope_reads_the_manifest_aid() -> None:
    a = aitp.AitpAgent.generate()
    b = aitp.AitpAgent.generate()
    b_manifest = b.build_manifest("b", "http://b/aitp/handshake/hello", ["demo.y"])
    a.build_manifest("a", "http://a/aitp/handshake/hello", ["demo.x"])

    hello = a.new_session().build_hello(b_manifest, ["demo.y"])
    assert peer_aid_from_hello_envelope(hello) == a.aid


def test_peer_aid_from_hello_envelope_is_none_on_garbage_json() -> None:
    assert peer_aid_from_hello_envelope("not json at all") is None


def test_peer_aid_from_hello_envelope_falls_back_to_a_string_sender() -> None:
    envelope = json.dumps({"payload": {}, "sender": "aid:pubkey:fallback"})
    assert peer_aid_from_hello_envelope(envelope) == "aid:pubkey:fallback"


def test_peer_aid_from_hello_envelope_is_none_when_shape_is_unexpected() -> None:
    """No manifest.aid AND no string sender (the real wire shape has `sender`
    as a dict, not a string) — the caller falls back to a no-op mint rather
    than crash."""
    envelope = json.dumps({"payload": {}, "sender": {"agent_id": "not-a-string-sender"}})
    assert peer_aid_from_hello_envelope(envelope) is None
