"""End-to-end smoke test of the aitp-py delegation bindings (RFC-AITP-0006).

Skipped if the `aitp` extension isn't importable (run `maturin develop`
in aitp-rs/bindings/aitp-py to install it into the playground venv).

Under AITP v0.2 every portable trust artifact (TCT, grant voucher,
delegation token) is an opaque compact-JWS string. A handshake completion
returns ``{"tct": "<jws>", "grant_voucher": "<jws>"}``; a delegation is
built from the *voucher*, not the TCT.
"""
from __future__ import annotations

import hashlib
import json

import pytest

aitp = pytest.importorskip("aitp")


def _agent(seed: bytes) -> "aitp.AitpAgent":
    return aitp.AitpAgent.from_seed(hashlib.sha256(seed).digest())


def _is_compact_jws(token: str) -> bool:
    return isinstance(token, str) and token.count(".") == 2


def _handshake(initiator, responder, responder_manifest, grants):
    """Run a full handshake; return the initiator's completion dict
    ``{"tct", "grant_voucher"}``."""
    sess = initiator.new_session()
    hello = sess.build_hello(responder_manifest, grants)
    rsess = responder.new_responder()
    ack, sid = rsess.process_hello(hello)
    commit = sess.process_hello_ack(ack, sid)
    commit_ack, _ = rsess.process_commit(commit)
    return json.loads(sess.complete(commit_ack))


def test_delegation_round_trip() -> None:
    """B holds A's TCT → B delegates to C → A verifies, mints fresh TCT for C
    → C self-verifies."""
    A = _agent(b"A")
    B = _agent(b"B")
    C = _agent(b"C")

    manifest_A = A.build_manifest(
        "A", "http://localhost:1/aitp/handshake/hello", ["write.content"],
    )
    B.build_manifest("B", "http://localhost:2/aitp/handshake/hello", ["x"])
    C.build_manifest("C", "http://localhost:3/aitp/handshake/hello", ["x"])

    # B initiates with A; A issues B a TCT + grant voucher for write.content.
    completed_b = _handshake(B, A, manifest_A, ["write.content"])
    voucher_b = completed_b["grant_voucher"]
    assert _is_compact_jws(voucher_b)

    # B delegates write.content to C, building from the voucher.
    delegation = B.build_delegation(voucher_b, C.aid, ["write.content"], 600)
    assert _is_compact_jws(delegation)

    # A verifies and mints a fresh TCT for C.
    verified = aitp.verify_delegation(delegation, A.aid)
    assert verified.delegator == A.aid
    assert verified.delegatee == C.aid
    assert verified.grants == ["write.content"]

    fresh = json.loads(A.issue_tct_for_delegatee(verified, 3600))
    fresh_tct = fresh["tct"]
    assert _is_compact_jws(fresh_tct)

    # C self-verifies the fresh TCT; the verified identity exposes the
    # issuer (A) and the delegated grants.
    identity = C.verify_tct(fresh_tct, "write.content")
    assert identity.peer_aid == A.aid
    assert identity.grants == ["write.content"]


def test_delegation_scope_exceeds_held_grants() -> None:
    """Builder rejects scopes that are not subsets of the voucher's grants."""
    A = _agent(b"A2")
    B = _agent(b"B2")
    C = _agent(b"C2")

    manifest_A = A.build_manifest(
        "A", "http://localhost:1/aitp/handshake/hello", ["write.content"],
    )
    B.build_manifest("B", "http://localhost:2/aitp/handshake/hello", ["x"])
    C.build_manifest("C", "http://localhost:3/aitp/handshake/hello", ["x"])

    voucher_b = _handshake(B, A, manifest_A, ["write.content"])["grant_voucher"]
    with pytest.raises(RuntimeError, match="exceeds"):
        B.build_delegation(voucher_b, C.aid, ["other.grant"], 600)


def test_verify_delegation_wrong_verifier_rejected() -> None:
    """Only the original delegator can redeem a delegation token."""
    A = _agent(b"A3")
    B = _agent(b"B3")
    C = _agent(b"C3")
    D = _agent(b"D3")  # stranger

    manifest_A = A.build_manifest(
        "A", "http://localhost:1/aitp/handshake/hello", ["x"],
    )
    B.build_manifest("B", "http://localhost:2/aitp/handshake/hello", ["x"])
    C.build_manifest("C", "http://localhost:3/aitp/handshake/hello", ["x"])
    D.build_manifest("D", "http://localhost:4/aitp/handshake/hello", ["x"])

    voucher_b = _handshake(B, A, manifest_A, ["x"])["grant_voucher"]
    delegation = B.build_delegation(voucher_b, C.aid, ["x"], 600)
    # D (not A) tries to verify — fails.
    with pytest.raises(RuntimeError):
        aitp.verify_delegation(delegation, D.aid)
