"""Interlock: the envelope signing input the installed `aitp-sdk` uses.

aitp-sdk 0.17 (protocol `aitp/0.3`, RFC-AITP-0001 §5.4) replaced the
envelope signing input:

    sig_input_v2 = "aitp-envelope-v2" || 0x00 || JCS(envelope minus "signature")
    signature    = base64url(Ed25519(sha256(sig_input_v2)))

The 0.16 input was the pipe string
`message_id|timestamp|sender.agent_id|hex(sha256(JCS(payload)))`, which left
`version`, `message_type` and `extensions` outside the signature.

This is the D-1 oracle-independence pattern (`DECISIONS.md`;
`test_pinned_key_proof_convention.py`, `test_revocation_signing_convention.py`):
the signing input is rebuilt here with the vendored canonicalizer
(`tests/unit/_jcs_reference.py`) and checked with `cryptography`, never with
the SDK that produced it. Then the direction is reversed: an envelope signed
here verifies under `aitp.verify_envelope`. Every negative case is shown to
fire by construction (D-2), on both sides.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

import aitp

from tests.unit._jcs_reference import canonicalize

# Literals, never read from `aitp`: the oracle must not take its convention
# from the SDK it checks.
PROTOCOL = "aitp/0.3"
DOMAIN_TAG = b"aitp-envelope-v2\x00"
SEED = bytes(range(32))


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _public_key(aid: str) -> Ed25519PublicKey:
    assert aid.startswith("aid:pubkey:"), aid
    return Ed25519PublicKey.from_public_bytes(_b64d(aid[len("aid:pubkey:"):]))


def _v2_digest(envelope: dict) -> bytes:
    unsigned = {k: v for k, v in envelope.items() if k != "signature"}
    return hashlib.sha256(DOMAIN_TAG + canonicalize(unsigned)).digest()


def _independently_verifies(envelope: dict, sender_aid: str) -> bool:
    sig = envelope["signature"]
    sig = sig.split(".", 1)[1] if sig.startswith("ed25519.") else sig
    try:
        _public_key(sender_aid).verify(_b64d(sig), _v2_digest(envelope))
    except InvalidSignature:
        return False
    return True


def _sdk_code(envelope: dict, sender_aid: str) -> str | None:
    """`None` when the SDK accepts the envelope, else its `.code`."""
    try:
        aitp.verify_envelope(json.dumps(envelope), sender_aid)
    except aitp.EnvelopeVerificationError as exc:
        return exc.code
    return None


@pytest.fixture(scope="module")
def agent() -> "aitp.AitpAgent":
    a = aitp.AitpAgent.from_seed(SEED)
    raw = Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes_raw()
    assert a.aid == "aid:pubkey:" + _b64e(raw), "seed→AID mapping changed"
    return a


@pytest.fixture
def sdk_envelope(agent) -> dict:
    """An SDK-signed envelope that carries `extensions`, so every member the
    v2 input newly covers is present."""
    return json.loads(agent.sign_envelope(
        "tct",
        json.dumps({"note": "interlock"}),
        extensions_json=json.dumps({"com.example.trace": {"id": "t-1"}}),
    ))


def _hand_signed(sender_aid: str, *, digest=_v2_digest, **overrides) -> dict:
    env = {
        "version": PROTOCOL,
        "message_type": "tct",
        "message_id": str(uuid.uuid4()),
        "timestamp": int(time.time()),
        "sender": {"agent_id": sender_aid},
        "payload": {"note": "hand-signed"},
        **overrides,
    }
    env["signature"] = _b64e(Ed25519PrivateKey.from_private_bytes(SEED).sign(digest(env)))
    return env


# ── SDK-signed → independent verify ─────────────────────────────────────────


def test_the_sdk_stamps_the_current_protocol(sdk_envelope) -> None:
    assert sdk_envelope["version"] == PROTOCOL


def test_an_sdk_envelope_verifies_under_the_independent_v2_input(agent, sdk_envelope) -> None:
    assert _independently_verifies(sdk_envelope, agent.aid)
    assert _sdk_code(sdk_envelope, agent.aid) is None


def test_a_real_hello_verifies_under_the_independent_v2_input(agent) -> None:
    """Not just `sign_envelope`: the handshake path signs the same way."""
    responder = aitp.AitpAgent.generate()
    manifest = responder.build_manifest("r", "http://r/aitp/handshake/hello", ["demo.x"])
    agent.build_manifest("i", "http://i/aitp/handshake/hello", ["demo.y"])
    hello = json.loads(
        agent.new_session(pinned_keys=[responder.aid]).build_hello(manifest, ["demo.x"])
    )
    assert hello["version"] == PROTOCOL
    assert _independently_verifies(hello, agent.aid)
    assert _sdk_code(hello, agent.aid) is None


@pytest.mark.parametrize(
    ("member", "value", "sdk_code"),
    [
        # A stale version is caught before the signature is even checked.
        ("version", "aitp/0.2", "UNKNOWN_VERSION"),
        # A registered type, so only the signature can catch the swap.
        ("message_type", "error", "INVALID_SIGNATURE"),
        ("extensions", {"com.example.trace": {"id": "t-2"}}, "INVALID_SIGNATURE"),
    ],
)
def test_members_the_v1_input_left_out_are_now_signed(
    agent, sdk_envelope, member, value, sdk_code,
) -> None:
    assert _independently_verifies(sdk_envelope, agent.aid)  # control
    tampered = {**sdk_envelope, member: value}
    assert not _independently_verifies(tampered, agent.aid)
    assert _sdk_code(tampered, agent.aid) == sdk_code


def test_dropping_an_empty_extensions_map_breaks_the_signature(agent) -> None:
    """Absent and `{}` are different signing inputs (upgrading-0.17 §2)."""
    env = json.loads(agent.sign_envelope("tct", json.dumps({"n": 1}), extensions_json="{}"))
    assert env["extensions"] == {}
    stripped = {k: v for k, v in env.items() if k != "extensions"}
    assert not _independently_verifies(stripped, agent.aid)
    assert _sdk_code(stripped, agent.aid) == "INVALID_SIGNATURE"


# ── hand-signed → SDK verify ────────────────────────────────────────────────


def test_a_hand_signed_v2_envelope_verifies_in_the_sdk(agent) -> None:
    env = _hand_signed(agent.aid, extensions={"com.example.trace": {"id": "h-1"}})
    assert _sdk_code(env, agent.aid) is None


def test_a_v1_pipe_input_signature_is_rejected(agent) -> None:
    def v1_digest(env: dict) -> bytes:
        pipe = "|".join([
            env["message_id"],
            str(env["timestamp"]),
            env["sender"]["agent_id"],
            hashlib.sha256(canonicalize(env["payload"])).hexdigest(),
        ])
        return hashlib.sha256(pipe.encode()).digest()

    env = _hand_signed(agent.aid, digest=v1_digest)
    assert not _independently_verifies(env, agent.aid)
    assert _sdk_code(env, agent.aid) == "INVALID_SIGNATURE"


def test_the_key_comes_from_the_expected_aid_not_the_envelope(agent) -> None:
    """A self-consistent envelope from someone else is not from `agent`."""
    other = aitp.AitpAgent.generate()
    env = json.loads(other.sign_envelope("tct", json.dumps({"n": 1})))
    assert _sdk_code(env, other.aid) is None  # control
    assert _sdk_code(env, agent.aid) == "INVALID_SIGNATURE"
