"""Interlock: what signing input does the installed `aitp-sdk` use for a
revocation snapshot?

This test exists because the answer changed once already, silently. Up to
0.4.x the SDK signed the JCS bytes of the **transport wrapper**
(`{"revocation_list": {...}}`); from 0.5.0 it signs the **inner body**
(`{...}`) alone. No dual-accept exists in either direction, so a wheel from
the wrong side of that line produces snapshots this repo's control plane
cannot verify — and vice versa. Nothing in this repo noticed, because nothing
in this repo verifies a snapshot signature at all yet (see the plan's Phase 6).

The oracle here is deliberately **not** the SDK. A test where the SDK both
signs and verifies passes under any self-consistent convention, including a
wrong one — which is precisely how the wrapped form survived a full release
across this family. Verification is done with `cryptography` plus an
independent RFC 8785 canonicalizer vendored in `_jcs_reference.py`; see that
file's header for why it is a copy.

Scope: **Ed25519 issuers only** — what the control plane issues
(`CP_AID_SEED_HEX`). P-256 AIDs carry a `p256.`-tagged signature and a
44-char identifier; they are not exercised here rather than half-supported.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Callable

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

import aitp

from tests.unit._jcs_reference import canonicalize

def test_the_sdk_exposes_the_signing_surface_this_module_interlocks() -> None:
    """A hard assertion, not a skipif — deliberately.

    `sign_revocation_list` is unconditional in the binding (no
    `#[cfg(feature)]`), and this repo floors `aitp-sdk>=0.6.0`, so a wheel
    without it is not one CI can resolve. The one path that still reaches
    here is `maturin develop` from an old sibling `aitp-rs` checkout, which
    bypasses the resolver entirely — and that is precisely where a silent
    skip is worst.

    A skip would also not be loud. CI runs `pytest -q` with no `addopts`, so
    a skip renders as a bare `s` and the reason string is never printed to
    anyone. An earlier version of this module claimed to "skip LOUDLY"; the
    configuration did not provide it. One named red test does.
    """
    assert hasattr(aitp.AitpAgent, "sign_revocation_list"), (
        "installed aitp-sdk has no AitpAgent.sign_revocation_list — the "
        "revocation signing-convention interlock cannot run, so this suite "
        "is a coverage hole rather than a pass"
    )


_AID_PREFIX = "aid:pubkey:"
_AID_ED25519_PREFIX = "aid:pubkey:ed25519:"


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _public_key_from_aid(aid: str) -> Ed25519PublicKey:
    """`aid:pubkey:<b64url(32-byte ed25519 pubkey)>` -> a usable public key.

    AITP AIDs are self-certifying: the raw public key is encoded in the
    identifier itself, so an issuer's key needs no lookup. Both the implicit
    form and the algorithm-tagged `ed25519:` form are accepted; a `p256:` AID
    is rejected rather than mis-parsed (see this module's docstring).
    """
    if aid.startswith(_AID_ED25519_PREFIX):
        identifier = aid[len(_AID_ED25519_PREFIX):]
    elif aid.startswith(_AID_PREFIX):
        identifier = aid[len(_AID_PREFIX):]
        if identifier.startswith("p256:"):
            raise AssertionError(f"P-256 AID is out of scope for this test: {aid}")
    else:
        raise AssertionError(f"not an aid:pubkey AID: {aid}")
    raw = _b64url_decode(identifier)
    assert len(raw) == 32, f"expected a 32-byte Ed25519 key, got {len(raw)}"
    return Ed25519PublicKey.from_public_bytes(raw)


# ── The signing inputs, named ────────────────────────────────────────────
#
# Named constructs rather than inline expressions, because the wrong ones are
# not dead code: they are the shapes being EXCLUDED, and an exclusion nobody
# writes down silently stops being tested.

SigningInput = Callable[[dict], bytes]

SIGNING_INPUTS: dict[str, SigningInput] = {
    # RFC-AITP-0008 §1.5 / RFC-AITP-0001 §5.4.1 — the correct one from 0.5.0.
    # The `revocation_list` key is transport routing metadata; `signature` is
    # a SIBLING of the body, never a member, so nothing is stripped before
    # canonicalizing.
    "inner_body": lambda env: canonicalize(env["revocation_list"]),
    # The pre-0.5.0 shape. Signing the transport wrapper.
    "wrapped": lambda env: canonicalize({"revocation_list": env["revocation_list"]}),
    # The shape that would result from copying the session bundle's convention
    # (RFC-AITP-0010 §3, where `signature` IS a body member) onto revocation.
    # Revocation keeps the sibling placement; this pins that it stays that way.
    "self_inclusive": lambda env: canonicalize(
        {**env["revocation_list"], "signature": env["signature"]}
    ),
}


def _assert_untagged_signature(signature: str) -> None:
    """An Ed25519 signature field is bare base64url — no algorithm tag.

    `Signature::algorithm` treats a `p256.` prefix as the only tag and defaults
    untagged to Ed25519 (`aitp-crypto/src/keys.rs:510-518`). Assert the absence
    rather than assume it: `base64.urlsafe_b64decode` does not validate, so a
    tagged signature would decode to garbage and surface as "does not sign the
    inner body" — a true failure with a misleading cause. Scope-widening to
    P-256 should fail *here*, naming the reason.
    """
    assert "." not in signature, (
        f"signature carries an algorithm tag ({signature.split('.', 1)[0]}.) — "
        "this module is Ed25519-only; see the module docstring."
    )


def _verifies_under(envelope: dict, signing_input: str, *, aid: str | None = None) -> bool:
    """Does `envelope["signature"]` verify over the named signing input?

    The SDK signs the SHA-256 **digest** of the canonical bytes, not the bytes
    themselves (`aitp-tct/src/revocation.rs`: `sign(&Sha256::digest(&canonical))`),
    so the digest is the Ed25519 message.
    """
    issuer = aid if aid is not None else envelope["revocation_list"]["issuer"]
    _assert_untagged_signature(envelope["signature"])
    pubkey = _public_key_from_aid(issuer)
    digest = hashlib.sha256(SIGNING_INPUTS[signing_input](envelope)).digest()
    try:
        pubkey.verify(_b64url_decode(envelope["signature"]), digest)
        return True
    except InvalidSignature:
        return False


@pytest.fixture
def minted_envelope() -> dict:
    """A snapshot minted by the *installed* SDK — the artifact under test."""
    agent = aitp.AitpAgent.generate()
    envelope = json.loads(
        agent.sign_revocation_list(
            [{"jti": "550e8400-e29b-41d4-a716-446655440000", "reason": "interlock"}],
            600,
        )
    )
    # Every expected value in this module is derived from this envelope. No
    # digest, signature, or canonical byte string is ever pasted in from
    # failure output — pinning program output as the expected value is the
    # bug class this test exists to remove.
    assert envelope["revocation_list"]["issuer"] == agent.aid
    return envelope


def test_revocation_snapshot_signature_is_over_the_inner_body_not_the_wrapper(
    minted_envelope: dict,
) -> None:
    """The interlock. If this fails, the installed SDK changed what it signs."""
    assert _verifies_under(minted_envelope, "inner_body"), (
        "the installed aitp-sdk does NOT sign the inner revocation_list body. "
        "If it signs the wrapper instead, this wheel is pre-0.5.0 and its "
        "snapshots will not verify against the control plane."
    )
    assert not _verifies_under(minted_envelope, "wrapped"), (
        "the installed aitp-sdk signs the TRANSPORT WRAPPER — the pre-0.5.0 "
        "convention. See the plan's Context: this is wire-incompatible with "
        "the control plane, in both directions."
    )
    assert not _verifies_under(minted_envelope, "self_inclusive"), (
        "the signature verified over a body containing itself. Revocation "
        "keeps `signature` as a sibling of the body (RFC-AITP-0001 §5.4.1); "
        "it is the session bundle that places it inside."
    )


def test_the_three_signing_inputs_are_actually_distinct(minted_envelope: dict) -> None:
    """No assertion above is accidentally aliasing another.

    Cheap, and it is the thing that would catch a canonicalizer or a lambda
    that made two of the three shapes collapse to identical bytes — which
    would turn a negative assertion into a restatement of the positive one.
    """
    produced = {name: fn(minted_envelope) for name, fn in SIGNING_INPUTS.items()}
    assert len(set(produced.values())) == len(SIGNING_INPUTS), (
        "two signing inputs canonicalized to the same bytes: "
        f"{ {k: len(v) for k, v in produced.items()} }"
    )


def test_the_wrapped_predicate_really_discriminates(minted_envelope: dict) -> None:
    """Non-vacuity proof for the negative assertion that matters most.

    A negative assertion that can never fire is worse than no assertion: it
    reads as coverage. So mint the *wrong* thing on purpose — a snapshot
    signed over the wrapped form, using a local key, exactly as a pre-0.5.0
    SDK would have produced — and show the predicates swap verdicts.

    This is done by construction rather than by reasoning, and it is why the
    `wrapped` entry is not dead code.
    """
    private = Ed25519PrivateKey.generate()
    raw_pub = private.public_key().public_bytes_raw()
    aid = _AID_PREFIX + base64.urlsafe_b64encode(raw_pub).decode().rstrip("=")

    # Reuse the real body shape, re-issued to the local key.
    body = {**minted_envelope["revocation_list"], "issuer": aid}
    wrapped_bytes = canonicalize({"revocation_list": body})
    signature = private.sign(hashlib.sha256(wrapped_bytes).digest())

    legacy_envelope = {
        "revocation_list": body,
        "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }

    assert _verifies_under(legacy_envelope, "wrapped"), (
        "the `wrapped` predicate failed to recognise an envelope that was "
        "definitively signed over the wrapped form — the predicate is broken, "
        "so the negative assertion in the interlock proves nothing."
    )
    assert not _verifies_under(legacy_envelope, "inner_body"), (
        "the `inner_body` predicate accepted a wrapped-form signature — the "
        "two shapes are not being distinguished at all."
    )


def test_self_inclusive_shape_is_not_constructible_by_a_signer(
    minted_envelope: dict,
) -> None:
    """Documents the one limit of the interlock, so nobody assumes otherwise.

    `wrapped` is shown non-vacuous by construction above. `self_inclusive`
    **cannot be**, and the reason is structural rather than an omission: a
    signature computed over a body that already contains that signature is a
    fixed point, not a shape any signer can produce. Its assertion is a guard
    against a *convention* change (someone generalizing the session bundle's
    member placement onto revocation), not against a forgeable artifact.

    What is checkable — and is checked — is that the shape differs from the
    real signing input, so the assertion is testing a distinct thing rather
    than restating `inner_body`. That is `test_the_three_signing_inputs_are_
    actually_distinct`; this test records *why* the stronger proof is absent.
    """
    body = minted_envelope["revocation_list"]
    assert "signature" not in body, (
        "the minted body carries a `signature` member — revocation's sibling "
        "placement has changed and this whole module needs revisiting against "
        "RFC-AITP-0001 §5.4.1."
    )
    assert SIGNING_INPUTS["self_inclusive"](minted_envelope) != SIGNING_INPUTS[
        "inner_body"
    ](minted_envelope)


def test_a_tagged_signature_is_rejected_rather_than_silently_mangled(
    minted_envelope: dict,
) -> None:
    """The guard against a misleading diagnostic on a future P-256 widening.

    `base64.urlsafe_b64decode` does not validate its input, so a `p256.`-tagged
    signature would decode to garbage and every predicate would return False —
    reporting "the SDK does not sign the inner body" when the real answer is
    "this module does not speak P-256". Assert the tag is caught first.
    """
    tagged = {**minted_envelope, "signature": "p256." + minted_envelope["signature"]}
    with pytest.raises(AssertionError, match="algorithm tag"):
        _verifies_under(tagged, "inner_body")


def test_the_vendored_canonicalizer_has_not_drifted_from_its_source() -> None:
    """`_jcs_reference.py` is a copy. Copies rot; this is the guard.

    It exists because `aitp-verifier` is not installable from PyPI, so the
    oracle had to be vendored rather than depended on. The risk that carries
    is not that the copy is wrong today — it is that the original changes and
    nobody notices, leaving this suite verifying against a stale RFC 8785
    implementation while believing it is independent.

    Skipped when the sibling checkout is absent on a developer machine — most
    developers legitimately have only this one checkout, and turning that into
    a red suite would not be a defect. In CI, absence is a hard failure: unlike
    D-11's three wheel-surface guards (which the floor makes unreachable),
    `ci.yml` clones `aitp-verifier-py` specifically to make this a real gate
    (`PENDING.md` P3's close-out mechanism), so a missing checkout there means
    the gate itself is gone, not that nothing needs guarding.
    """
    import os
    import pathlib

    # This path agrees with ci.yml's clone location only because ci.yml
    # checks this repo out with no `path:` input (so `$GITHUB_WORKSPACE` IS
    # this repo's root, and the sibling lands in its parent). docker.yml
    # already uses a different sibling-layout convention (`path:
    # aitp-playground`); if ci.yml ever adopts it, this resolution and the
    # clone step diverge and this guard starts skipping in CI silently.
    source = (
        pathlib.Path(__file__).resolve().parents[2].parent
        / "aitp-verifier-py"
        / "aitp_verifier"
        / "jcs.py"
    )
    if not source.exists():
        assert not os.environ.get("CI"), (
            f"sibling checkout absent at {source} — ci.yml clones "
            "aitp-verifier-py before pytest, so in CI this means the clone "
            "step is gone or the layout changed; the drift guard is NOT "
            "passing, it is not running"
        )
        pytest.skip(f"sibling checkout not present at {source}")

    vendored = pathlib.Path(__file__).resolve().parent / "_jcs_reference.py"

    def _body(text: str) -> str:
        """Everything from the first import on — i.e. the code, not the header.

        The vendored file deliberately replaces the module docstring with a
        provenance note explaining why it is a copy, so the docstrings differ
        by design. The code below must not.
        """
        marker = "from __future__ import annotations"
        assert marker in text, "expected a __future__ import to anchor on"
        return text[text.index(marker):]

    assert _body(vendored.read_text()) == _body(source.read_text()), (
        f"{vendored.name} has drifted from {source} — the oracle this suite "
        "verifies against is no longer the implementation it was copied from. "
        "Re-copy it (keeping the provenance header) or, if aitp-verifier has "
        "since been published to PyPI, delete the copy and take a dev-group "
        "dependency instead."
    )
