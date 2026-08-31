"""Interlock: what encoding does the installed `aitp-sdk` use for the
pinned-key proof-of-possession `timestamp` field?

This is the second instance of the D-1 oracle-independence pattern (see
`test_revocation_signing_convention.py`, this module's template), applied to
a different artifact: the pinned-key HELLO proof instead of the revocation
snapshot signature.

RFC-AITP-0002 §3.1's own prose once called the field
`timestamp_be_8_bytes` — an 8-byte big-endian signed 64-bit integer. The only
concrete signed vector in the conformance pack only verifies when the
timestamp is instead its base-10 ASCII-decimal string, matching how
`message_id` is already string-encoded (see `kat-pinned-key-proof-001`'s
`$comment` in `../aitp-rs/tests/schemas/known-answer/jcs-sha256.json`, the
erratum this test exists to keep caught). This repo's own installed
`aitp-sdk 0.10.0` wheel was measured, during planning, to sign the live leg
of this exact proof under the big-endian convention — the wire-incompatible,
pre-erratum-fix encoding — and only started signing ASCII-decimal once the
floor moved to `aitp-sdk>=0.11.0` (Phase 1 of
`plans/aitp-rs-breaking-changes-adoption.md`). Nothing in this repo noticed
that regression on its own, because nothing here decoded a pinned-key proof
independently of the SDK that minted it — precisely the D-1 blind spot.

The oracle here is deliberately **not** the SDK (D-1,
`DECISIONS.md`): `aitp-py` does not expose `pinned_key_proof_input` /
`verify_pinned_key` to Python at all, so this test cannot even ask the SDK to
grade its own homework. Instead it observes the HELLO envelope the installed
wheel actually emits, reconstructs `proof_input` in pure Python per
RFC-AITP-0002 §3.1, and verifies the embedded Ed25519 signature with the
`cryptography` library. Every negative assertion is shown to fire by
construction, never asserted from reasoning alone (D-2).

Scope, matching the boundary `test_revocation_signing_convention.py` draws
for its own artifact:

- **Ed25519 only.** P-256 agents cannot use pinned-key identity at all
  (`bindings/aitp-py/src/agent.rs:144` in `../aitp-rs` rejects it), so there
  is nothing to half-support here.
- **Initiator side only** (the HELLO message's own proof, i.e.
  `payload.identity.proof` on the envelope `build_hello` returns). The
  responder's HELLO_ACK is not covered by this fixture and is left for a
  separate module if it is ever needed — this one's fixture only drives one
  handshake step.

The SDK surface this module interlocks (`AitpAgent.generate`,
`build_manifest`, `new_session`, `Session.build_hello`) is asserted with a
hard `assert`, not a `skipif`. Handshake construction is unconditional in the
binding, and this repo floors `aitp-sdk>=0.11.0`, so a wheel missing any of
this surface is a broken wheel, not an opted-out feature — the same D-11
position `test_revocation_signing_convention.py` already applies to its own
surface, not a new one invented here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import aitp

# ── KAT vector `kat-pinned-key-proof-001` ───────────────────────────────────
#
# Copied verbatim from
# ../aitp-rs/tests/schemas/known-answer/jcs-sha256.json (byte-identical to
# ../agentidentitytrustprotocol/schemas/conformance/known-answer/jcs-sha256.json)
# rather than read at runtime — a test that self-skips when a sibling
# checkout is missing is a coverage hole this repo has already been bitten by
# once (see this module's docstring and D-1's sibling, the revocation
# module's `test_the_vendored_canonicalizer_has_not_drifted_from_its_source`,
# which explicitly does NOT apply to this file: there is no long-lived
# vendored *algorithm* here to drift, only these frozen literals).
KAT_SENDER_AID = "aid:pubkey:dqFZIESm5PURJlvKc6YE2QsFKdHfYCvjChmpJXZg0fU"
KAT_RECEIVER_AID = "aid:pubkey:O2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik"
KAT_MESSAGE_ID = "770e8400-e29b-41d4-a716-446655440701"
KAT_TIMESTAMP = 1711900000
KAT_POP_NONCE = "AAAAAAAAAAAAAAAAAAAAAA"
KAT_PROOF_INPUT_HEX = (
    "616974702d70696e6e65642d6b65792d7631006169643a7075626b65793a"
    "6471465a4945536d355055524a6c764b63365945325173464b6448665943"
    "766a43686d704a585a67306655006169643a7075626b65793a4f326f6e76"
    "4d3632704331696f366a514b6d384e6332557946586364346b4f6d4f7342"
    "496f59745a32696b0037373065383430302d653239622d343164342d6137"
    "31362d343436363535343430373031003137313139303030303000000000"
    "00000000000000000000000000"
)
KAT_PROOF_INPUT_LEN_BYTES = 193
KAT_SHA256_HEX = "061ea87d1cc52c860fbeece9f6ba266669ebb0581a0e0ed409267690c39edb09"
KAT_SHA256_B64URL = "Bh6ofRzFLIYPvuzp9romZmnrsFgaDg7UCSZ2kMOe2wk"
KAT_SIGNING_KEYPAIR_ID = "kat-keypair-003"
KAT_SIGNER_AID = "aid:pubkey:dqFZIESm5PURJlvKc6YE2QsFKdHfYCvjChmpJXZg0fU"
KAT_SIGNATURE_B64URL = (
    "vdLCEPGuUvVryXxnPdo_ZNYqaZmGuByZ5kRMU0ikKeVbn3SUjj2DD7y8e5pNBtp7jS"
    "HZ-Z0wzNdOl0NS1mSpCQ"
)

assert KAT_SIGNER_AID == KAT_SENDER_AID, (
    "the KAT's signer_aid is documented as equal to sender_aid — the AID "
    "whose public key verifies the signature. If this ever legitimately "
    "diverges, every _public_key_from_aid(sender_aid) call below needs to "
    "switch to a distinct signer_aid instead."
)


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _public_key_from_aid(aid: str) -> Ed25519PublicKey:
    """`aid:pubkey:<b64url(32-byte ed25519 pubkey)>` -> a usable public key.

    AITP AIDs are self-certifying, so no lookup is needed. Scoped to
    Ed25519 only (see module docstring); a `p256:`-tagged AID is rejected
    rather than mis-parsed.
    """
    prefix = "aid:pubkey:"
    assert aid.startswith(prefix), f"not an aid:pubkey AID: {aid}"
    identifier = aid[len(prefix):]
    if identifier.startswith("p256:") or identifier.startswith("ed25519:"):
        # This module only ever mints ed25519 agents and only reads the
        # implicit (untagged) AID form; an algorithm-tagged identifier here
        # would mean the SDK changed its AID formatting, not that this is a
        # P-256 key to half-support.
        if identifier.startswith("p256:"):
            raise AssertionError(f"P-256 AID is out of scope for this test: {aid}")
        identifier = identifier[len("ed25519:"):]
    raw = _b64url_decode(identifier)
    assert len(raw) == 32, f"expected a 32-byte Ed25519 key, got {len(raw)}"
    return Ed25519PublicKey.from_public_bytes(raw)


def _proof_input_ascii_decimal(
    *, sender_aid: str, receiver_aid: str, message_id: str, timestamp: int, pop_nonce: str
) -> bytes:
    """RFC-AITP-0002 §3.1, the erratum-fixed (post-0.11.0) encoding.

    `timestamp` is its base-10 ASCII-decimal string — the same string-encoded
    treatment `message_id` already gets — not a fixed-width integer.
    """
    return (
        b"aitp-pinned-key-v1\0"
        + sender_aid.encode()
        + b"\0"
        + receiver_aid.encode()
        + b"\0"
        + message_id.encode()
        + b"\0"
        + str(timestamp).encode()
        + b"\0"
        + _b64url_decode(pop_nonce)
    )


def _proof_input_big_endian(
    *, sender_aid: str, receiver_aid: str, message_id: str, timestamp: int, pop_nonce: str
) -> bytes:
    """The pre-erratum encoding this test exists to reject: `timestamp` as
    an 8-byte big-endian signed 64-bit integer (`struct.pack(">q", ts)`),
    per RFC-AITP-0002 §3.1's original (erroneous) prose.
    """
    return (
        b"aitp-pinned-key-v1\0"
        + sender_aid.encode()
        + b"\0"
        + receiver_aid.encode()
        + b"\0"
        + message_id.encode()
        + b"\0"
        + struct.pack(">q", timestamp)
        + b"\0"
        + _b64url_decode(pop_nonce)
    )


def _verifies(
    *, signature_b64url: str, verifying_aid: str, proof_input: bytes
) -> bool:
    """Does `signature_b64url` verify over SHA-256(`proof_input`)?

    The signature is over the digest, not the raw proof_input bytes — the
    KAT's own `sha256_hex`/`sha256_b64url` fields exist to pin exactly that
    intermediate step, and the live leg below re-derives it the same way.
    """
    pubkey = _public_key_from_aid(verifying_aid)
    digest = hashlib.sha256(proof_input).digest()
    try:
        pubkey.verify(_b64url_decode(signature_b64url), digest)
        return True
    except InvalidSignature:
        return False


# ── 1. The SDK surface this module interlocks ───────────────────────────────


def test_the_sdk_exposes_the_handshake_surface_this_module_interlocks() -> None:
    """A hard assertion, not a skipif — deliberately (D-11; see module
    docstring). Handshake construction is unconditional in the binding, and
    this repo floors `aitp-sdk>=0.11.0`, so a wheel missing this surface is
    a broken wheel — worth a loud, named red test, not a silent skip.
    """
    assert hasattr(aitp.AitpAgent, "generate"), "aitp.AitpAgent.generate is missing"
    assert hasattr(aitp.AitpAgent, "build_manifest"), (
        "aitp.AitpAgent.build_manifest is missing"
    )
    assert hasattr(aitp.AitpAgent, "new_session"), (
        "aitp.AitpAgent.new_session is missing — the pinned-key proof "
        "interlock cannot run, so this suite is a coverage hole rather "
        "than a pass"
    )
    probe = aitp.AitpAgent.generate(suite="ed25519")
    probe.build_manifest("probe", "http://localhost:9/aitp/handshake/hello", ["x"])
    session_cls = type(probe.new_session())
    assert hasattr(session_cls, "build_hello"), (
        "the session object returned by AitpAgent.new_session() has no "
        "build_hello — the pinned-key proof interlock cannot run"
    )


# ── 2. KAT leg: the reconstruction is validated before it is trusted ────────


def test_kat_reconstruction_matches_the_spec_vector_byte_for_byte() -> None:
    """`kat-pinned-key-proof-001` — validate the reconstruction itself
    before using it as an oracle for the live leg below. If this test is
    wrong, everything downstream of it is meaningless.
    """
    proof_input = _proof_input_ascii_decimal(
        sender_aid=KAT_SENDER_AID,
        receiver_aid=KAT_RECEIVER_AID,
        message_id=KAT_MESSAGE_ID,
        timestamp=KAT_TIMESTAMP,
        pop_nonce=KAT_POP_NONCE,
    )
    assert len(proof_input) == KAT_PROOF_INPUT_LEN_BYTES == 193
    assert proof_input.hex() == KAT_PROOF_INPUT_HEX, (
        "the ASCII-decimal proof_input reconstruction does not reproduce "
        "the KAT byte-for-byte"
    )

    digest = hashlib.sha256(proof_input).digest()
    assert digest.hex() == KAT_SHA256_HEX
    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == KAT_SHA256_B64URL

    assert _verifies(
        signature_b64url=KAT_SIGNATURE_B64URL,
        verifying_aid=KAT_SIGNER_AID,
        proof_input=proof_input,
    ), (
        "the pinned KAT signature does not verify over the ASCII-decimal "
        "proof_input — the reconstruction is wrong and cannot be trusted "
        "as an oracle for the live leg"
    )


def test_kat_big_endian_variant_is_shorter_and_does_not_verify() -> None:
    """Non-vacuity proof for the KAT's own negative assertion (D-2): the
    `$comment` on `kat-pinned-key-proof-001` states implementations MUST
    confirm the pinned signature does NOT verify under the big-endian
    encoding. Demonstrated here by construction, not by reasoning.
    """
    proof_input_be = _proof_input_big_endian(
        sender_aid=KAT_SENDER_AID,
        receiver_aid=KAT_RECEIVER_AID,
        message_id=KAT_MESSAGE_ID,
        timestamp=KAT_TIMESTAMP,
        pop_nonce=KAT_POP_NONCE,
    )
    # 8 bytes big-endian vs. 10 ASCII digits for this particular timestamp.
    assert len(proof_input_be) == KAT_PROOF_INPUT_LEN_BYTES - 2 == 191
    assert not _verifies(
        signature_b64url=KAT_SIGNATURE_B64URL,
        verifying_aid=KAT_SIGNER_AID,
        proof_input=proof_input_be,
    ), (
        "the KAT signature verified under the big-endian encoding — the "
        "KAT's MUST-NOT does not hold, so this predicate cannot be trusted "
        "to discriminate the live leg either"
    )


# ── 3. Live leg: what does the *installed* wheel actually mint? ─────────────


@pytest.fixture
def minted_hello() -> dict:
    """A real in-process HELLO handshake step, minted by the *installed*
    SDK — the artifact under test. Both agents build a pinned-key manifest
    (the `build_manifest` default) — **including the initiator**, since
    `new_session()` raises `RuntimeError` otherwise per the plan's own
    empirical note.
    """
    initiator = aitp.AitpAgent.generate(suite="ed25519")
    responder = aitp.AitpAgent.generate(suite="ed25519")

    initiator.build_manifest(
        "initiator", "http://localhost:1/aitp/handshake/hello", ["probe"]
    )
    responder_manifest_json = responder.build_manifest(
        "responder", "http://localhost:2/aitp/handshake/hello", ["probe"]
    )

    session = initiator.new_session()
    hello_json = session.build_hello(responder_manifest_json, ["probe"])
    envelope = json.loads(hello_json)

    # Nothing pasted from failure output — every expected value below is
    # either the KAT's literal or derived from this envelope / these agents.
    assert envelope["sender"]["agent_id"] == initiator.aid
    return {
        "envelope": envelope,
        "initiator_aid": initiator.aid,
        "responder_aid": responder.aid,
    }


def _proof_material(minted: dict) -> dict:
    """Pull proof material out of the HELLO envelope at its actual
    locations (not where a naive reading of the RFC would guess):
    `message_id`/`timestamp` are top-level on the envelope; `pop_nonce` and
    `identity.{proof,public_key}` are under `payload`; `sender_aid` is
    `envelope["sender"]["agent_id"]`; `receiver_aid` is the *peer's* AID —
    it is not present in the envelope's own header at all.
    """
    envelope = minted["envelope"]
    payload = envelope["payload"]
    identity = payload["identity"]
    assert identity["type"] == "pinned_key", (
        "the minted HELLO's identity.type is not pinned_key — this fixture "
        "relies on build_manifest()'s pinned-key default; if that default "
        "ever changes, fail here by name rather than downstream via a "
        "confusing signature mismatch"
    )
    return {
        "sender_aid": envelope["sender"]["agent_id"],
        "receiver_aid": minted["responder_aid"],
        "message_id": envelope["message_id"],
        "timestamp": envelope["timestamp"],
        "pop_nonce": payload["pop_nonce"],
        "signature_b64url": identity["proof"],
        "public_key_b64url": identity["public_key"],
    }


def test_live_leg_verifies_under_the_ascii_decimal_reconstruction(
    minted_hello: dict,
) -> None:
    """The interlock. If this fails, the installed SDK has stopped signing
    the pinned-key proof the erratum-fixed (RFC-AITP-0002 §3.1) way.
    """
    material = _proof_material(minted_hello)
    assert material["sender_aid"] == minted_hello["initiator_aid"]

    # The embedded public_key must match the sender AID's own key — belt and
    # suspenders before trusting it as the verification key below.
    embedded_pubkey_aid = "aid:pubkey:" + material["public_key_b64url"]
    assert embedded_pubkey_aid == material["sender_aid"], (
        "payload.identity.public_key does not match the sender's own AID"
    )

    proof_input = _proof_input_ascii_decimal(
        sender_aid=material["sender_aid"],
        receiver_aid=material["receiver_aid"],
        message_id=material["message_id"],
        timestamp=material["timestamp"],
        pop_nonce=material["pop_nonce"],
    )
    assert _verifies(
        signature_b64url=material["signature_b64url"],
        verifying_aid=material["sender_aid"],
        proof_input=proof_input,
    ), (
        "the installed aitp-sdk does NOT sign the pinned-key proof under "
        "the ASCII-decimal timestamp encoding. If it signs the big-endian "
        "encoding instead, this wheel predates the RFC-AITP-0002 §3.1 "
        "erratum fix and its proofs will not verify against a corrected "
        "peer — see this module's docstring."
    )


def test_live_leg_does_not_verify_under_the_big_endian_reconstruction(
    minted_hello: dict,
) -> None:
    """Per D-2, the negative assertion must be shown to fire — this is the
    assertion that would have caught the regression this module exists for.
    Demonstrated against the actual live proof, not just against the KAT.
    """
    material = _proof_material(minted_hello)
    proof_input_be = _proof_input_big_endian(
        sender_aid=material["sender_aid"],
        receiver_aid=material["receiver_aid"],
        message_id=material["message_id"],
        timestamp=material["timestamp"],
        pop_nonce=material["pop_nonce"],
    )
    assert not _verifies(
        signature_b64url=material["signature_b64url"],
        verifying_aid=material["sender_aid"],
        proof_input=proof_input_be,
    ), (
        "the live proof verified under the big-endian reconstruction — "
        "the installed wheel is signing the pre-erratum-fix encoding"
    )


def test_reconstructions_are_actually_distinct(minted_hello: dict) -> None:
    """Cheap non-aliasing check: the ASCII-decimal and big-endian inputs
    must not collapse to the same bytes for a real minted timestamp, or the
    positive/negative pair above would be restating one assertion twice.
    """
    material = _proof_material(minted_hello)
    ascii_input = _proof_input_ascii_decimal(
        sender_aid=material["sender_aid"],
        receiver_aid=material["receiver_aid"],
        message_id=material["message_id"],
        timestamp=material["timestamp"],
        pop_nonce=material["pop_nonce"],
    )
    be_input = _proof_input_big_endian(
        sender_aid=material["sender_aid"],
        receiver_aid=material["receiver_aid"],
        message_id=material["message_id"],
        timestamp=material["timestamp"],
        pop_nonce=material["pop_nonce"],
    )
    assert ascii_input != be_input
