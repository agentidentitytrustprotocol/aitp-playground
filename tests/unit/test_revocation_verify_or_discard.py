"""An unverifiable revocation snapshot is discarded. Not applied, not merged.

RFC-AITP-0008 §1.5 makes this a MUST, so it has no mode knob: whatever
`revocation_fail_mode` ends up governing, it governs the *absence* of a fresh
snapshot, never its authenticity. Under `soft_fail` a forged snapshot must
still be discarded — collapsing those two axes is how `aitp_verifier`'s single
switch ends up reporting a forged snapshot as not-revoked, and D1 rejects it.

Every forged input here is **minted**, never pasted from failure output, and
the signer for the negative cases is `cryptography` rather than the SDK — a
test where the SDK both signs and verifies passes under any self-consistent
convention, including a wrong one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import aitp

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

from revocation_state import RevocationState  # noqa: E402

from tests.unit._jcs_reference import canonicalize  # noqa: E402

_HAS_VERIFY = hasattr(aitp, "verify_revocation_list")

pytestmark = pytest.mark.skipif(
    not _HAS_VERIFY,
    reason=(
        "installed aitp-sdk has no verify_revocation_list (needs >=0.6.0) — "
        "the verify-or-discard path CANNOT BE TESTED against this wheel. "
        "This is a coverage hole, not a pass."
    ),
)


def _jti(label: str) -> str:
    """A stable UUID per label.

    `jti` is a `Uuid` in the signed body (`aitp-tct`'s `RevocationEntry`), so a
    readable placeholder like "jti-1" fails deserialization as `malformed`
    before verification is even reached — which would make several of the
    assertions below pass for entirely the wrong reason.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_OID, label))


def _snapshot(entries, *, issuer_key=None, published_at=None, ttl=3600, sign_wrapped=False):
    """Mint a snapshot with a local key, optionally the pre-0.5.0 way."""
    key = issuer_key or Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    aid = "aid:pubkey:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    now = int(time.time()) if published_at is None else published_at
    body: dict[str, Any] = {
        "version": "aitp/0.2",
        "issuer": aid,
        "published_at": now,
        "expires_at": now + ttl,
        "entries": [{"jti": j, "revoked_at": now} for j in entries],
    }
    signing_input = canonicalize({"revocation_list": body} if sign_wrapped else body)
    sig = key.sign(hashlib.sha256(signing_input).digest())
    envelope = {
        "revocation_list": body,
        "signature": base64.urlsafe_b64encode(sig).decode().rstrip("="),
    }
    return aid, json.dumps(envelope)


def _apply(state: RevocationState, envelope_json: str, expected_issuer: str) -> str | None:
    """The ingest decision, isolated: verify, then apply or discard.

    Mirrors `/admin/refresh-revocations`. Returns the discard cause, or None on
    a successful apply.
    """
    try:
        aitp.verify_revocation_list(envelope_json, expected_issuer)
    except Exception as exc:  # noqa: BLE001
        return getattr(exc, "code", None) or "signature_invalid"
    body = json.loads(envelope_json)["revocation_list"]
    state.apply_snapshot(
        [e["jti"] for e in body["entries"]],
        published_at=int(body["published_at"]),
        expires_at=int(body["expires_at"]),
    )
    return None


def test_a_genuine_snapshot_is_applied() -> None:
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")])
    assert _apply(state, env, aid) is None
    assert state.is_revoked(_jti("jti-1"))


def test_tampered_entries_are_discarded_and_the_deny_set_is_untouched() -> None:
    """Acceptance cause 1. The deny-set must not move at all."""
    state = RevocationState()
    aid, env = _snapshot([_jti("good")])
    _apply(state, env, aid)

    forged = json.loads(env)
    forged["revocation_list"]["entries"].append({"jti": _jti("injected"), "revoked_at": 0})

    assert _apply(state, json.dumps(forged), aid) == "signature_invalid"
    assert not state.is_revoked(_jti("injected")), "a forged entry reached the deny-set"
    assert state.is_revoked(_jti("good")), "the previously verified snapshot was dropped"


def test_a_wrapped_form_signature_is_discarded() -> None:
    """Acceptance cause 2 — the pre-0.5.0 convention, rejected at ingest."""
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")], sign_wrapped=True)
    assert _apply(state, env, aid) == "signature_invalid"
    assert not state.is_revoked(_jti("jti-1"))


def test_a_correctly_self_signed_snapshot_from_the_wrong_issuer_is_discarded() -> None:
    """Acceptance cause 3 — the one that proves the issuer pin does work.

    This snapshot is internally perfect: well-formed, unexpired, and signed by
    the key its own `issuer` names. It is rejected *only* because that issuer
    is not the one we pinned. Without the pin it would sail through, which is
    why verification without a pinned expected issuer is close to worthless.
    """
    state = RevocationState()
    pinned_aid, _ = _snapshot([])
    attacker_aid, attacker_env = _snapshot([_jti("injected")])

    assert attacker_aid != pinned_aid
    # Self-consistent: it verifies against its own issuer.
    assert _apply(RevocationState(), attacker_env, attacker_aid) is None

    assert _apply(state, attacker_env, pinned_aid) == "issuer_mismatch"
    assert not state.is_revoked(_jti("injected"))


def test_an_expired_snapshot_is_discarded_and_previous_entries_stay_enforced() -> None:
    """Acceptance cause 4."""
    state = RevocationState()
    key = Ed25519PrivateKey.generate()
    aid, fresh = _snapshot([_jti("still-revoked")], issuer_key=key)
    _apply(state, fresh, aid)

    _, stale = _snapshot([_jti("new")], issuer_key=key, published_at=int(time.time()) - 7200, ttl=3600)
    assert _apply(state, stale, aid) == "expired"
    assert state.is_revoked(_jti("still-revoked")), (
        "an expired snapshot discarded the previously verified one"
    )
    assert not state.is_revoked(_jti("new"))


def test_an_unknown_version_is_discarded_with_its_own_cause() -> None:
    """Cause 5 — and it must not surface as a signature failure.

    The version check runs first, so a snapshot from a future protocol reports
    what is actually wrong rather than sending someone hunting a key mismatch.
    """
    state = RevocationState()
    key = Ed25519PrivateKey.generate()
    aid, env = _snapshot([_jti("jti-1")], issuer_key=key)
    doc = json.loads(env)
    doc["revocation_list"]["version"] = "aitp/9.9"
    # Re-sign so the ONLY defect is the version.
    signing_input = canonicalize(doc["revocation_list"])
    doc["signature"] = base64.urlsafe_b64encode(
        key.sign(hashlib.sha256(signing_input).digest())
    ).decode().rstrip("=")

    assert _apply(state, json.dumps(doc), aid) == "version_unknown"
    assert not state.is_revoked(_jti("jti-1"))


def test_malformed_input_is_discarded() -> None:
    state = RevocationState()
    aid, _ = _snapshot([])
    assert _apply(state, "not json at all", aid) is not None
    assert len(state) == 0


def test_an_empty_verified_snapshot_is_applied_not_treated_as_suspect() -> None:
    """RFC-AITP-0008 §1.5: empty lists are signed, and mean something.

    A control plane whose DB read fails publishes an empty *signed* list. That
    snapshot verifies and MUST be accepted — the suppression window there is
    the CP's to own. Treating empty as suspect playground-side would be
    inventing a policy the spec does not have.
    """
    state = RevocationState()
    key = Ed25519PrivateKey.generate()
    aid, first = _snapshot([_jti("was-revoked")], issuer_key=key)
    _apply(state, first, aid)
    assert state.is_revoked(_jti("was-revoked"))

    _, empty = _snapshot([], issuer_key=key)
    assert _apply(state, empty, aid) is None
    assert not state.is_revoked(_jti("was-revoked"))
    assert state.snapshot is not None, "an empty snapshot is still a snapshot"


def test_discarding_never_clears_a_local_revocation() -> None:
    """Axis A must not touch the operator's own denials, in any failure mode."""
    state = RevocationState()
    state.revoke_local(_jti("mine"))
    aid, _ = _snapshot([])
    _, attacker_env = _snapshot([_jti("injected")])

    assert _apply(state, attacker_env, aid) == "issuer_mismatch"
    assert state.is_revoked(_jti("mine"))
