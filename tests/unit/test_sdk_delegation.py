"""End-to-end smoke test of the aitp-py delegation bindings (RFC-AITP-0006).

Skipped if the `aitp` extension isn't importable (run `maturin develop`
in aitp-rs/bindings/aitp-py to install it into the playground venv).
"""
from __future__ import annotations

import hashlib
import json

import pytest

aitp = pytest.importorskip("aitp")


def _agent(seed: bytes) -> "aitp.AitpAgent":
    return aitp.AitpAgent.from_seed(hashlib.sha256(seed).digest())


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

    # B initiates with A; A issues B a TCT for write.content.
    sess_b = B.new_session()
    hello = sess_b.build_hello(manifest_A, ["write.content"])
    responder_a = A.new_responder()
    ack, sid = responder_a.process_hello(hello)
    commit = sess_b.process_hello_ack(ack, sid)
    commit_ack, _ = responder_a.process_commit(commit)
    held_tct_b = sess_b.complete(commit_ack)

    # B delegates write.content to C.
    c_pk_b64u = C.aid.split(":")[-1]
    delegation = B.build_delegation(held_tct_b, C.aid, c_pk_b64u, ["write.content"], 600)
    assert "delegation" in json.loads(delegation)

    # A verifies and mints fresh TCT for C.
    verified = aitp.verify_delegation(delegation, A.aid)
    assert verified.delegator == A.aid
    assert verified.delegatee == C.aid
    assert verified.grants == ["write.content"]

    fresh = A.issue_tct_for_delegatee(verified, 3600)
    parsed = json.loads(fresh)["tct"]
    assert parsed["issuer"] == A.aid
    assert parsed["subject"] == C.aid
    assert parsed["grants"] == ["write.content"]

    # C self-verifies the fresh TCT.
    identity = C.verify_tct(fresh, "write.content")
    assert identity.peer_aid == A.aid
    assert identity.grants == ["write.content"]


def test_delegation_scope_exceeds_held_grants() -> None:
    """Builder rejects scopes that are not subsets of the held TCT's grants."""
    A = _agent(b"A2")
    B = _agent(b"B2")
    C = _agent(b"C2")

    A.build_manifest("A", "http://localhost:1/aitp/handshake/hello", ["write.content"])
    B.build_manifest("B", "http://localhost:2/aitp/handshake/hello", ["x"])
    C.build_manifest("C", "http://localhost:3/aitp/handshake/hello", ["x"])

    sess_b = B.new_session()
    hello = sess_b.build_hello(
        A.build_manifest("A", "http://localhost:1/aitp/handshake/hello", ["write.content"]),
        ["write.content"],
    )
    responder_a = A.new_responder()
    ack, sid = responder_a.process_hello(hello)
    commit = sess_b.process_hello_ack(ack, sid)
    commit_ack, _ = responder_a.process_commit(commit)
    held_tct_b = sess_b.complete(commit_ack)

    c_pk_b64u = C.aid.split(":")[-1]
    with pytest.raises(RuntimeError, match="exceeds"):
        B.build_delegation(held_tct_b, C.aid, c_pk_b64u, ["other.grant"], 600)


def test_verify_delegation_wrong_verifier_rejected() -> None:
    """Only the original delegator can redeem a delegation token."""
    A = _agent(b"A3")
    B = _agent(b"B3")
    C = _agent(b"C3")
    D = _agent(b"D3")  # stranger

    A.build_manifest("A", "http://localhost:1/aitp/handshake/hello", ["x"])
    B.build_manifest("B", "http://localhost:2/aitp/handshake/hello", ["x"])
    C.build_manifest("C", "http://localhost:3/aitp/handshake/hello", ["x"])
    D.build_manifest("D", "http://localhost:4/aitp/handshake/hello", ["x"])

    sess_b = B.new_session()
    hello = sess_b.build_hello(
        A.build_manifest("A", "http://localhost:1/aitp/handshake/hello", ["x"]),
        ["x"],
    )
    responder_a = A.new_responder()
    ack, sid = responder_a.process_hello(hello)
    commit = sess_b.process_hello_ack(ack, sid)
    commit_ack, _ = responder_a.process_commit(commit)
    held = sess_b.complete(commit_ack)

    delegation = B.build_delegation(
        held, C.aid, C.aid.split(":")[-1], ["x"], 600,
    )
    # D (not A) tries to verify — fails.
    with pytest.raises(RuntimeError):
        aitp.verify_delegation(delegation, D.aid)
