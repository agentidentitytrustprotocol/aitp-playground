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

Every assertion below drives the real `revocation_refresh.refresh_revocations`
— the single production ingest — with only the network transport stubbed.
`_apply` is NOT a reimplementation of the verify/discard decision; that
decision, and the two pre-flight guards ahead of it, are the module's own
code. See `revocation_refresh.py`'s own module docstring for why a second
hand-rolled copy here would be exactly the defect this file exists to guard
against.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import aitp

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import revocation_refresh  # noqa: E402
from revocation_state import RevocationState  # noqa: E402

from tests.unit._jcs_reference import canonicalize  # noqa: E402


def test_the_sdk_exposes_the_verify_surface_this_module_depends_on() -> None:
    """A hard assertion rather than a skipif — same reasoning as the
    signing-convention interlock: the floor makes the condition unreachable
    in CI, the one path that reaches it (`maturin develop` from an old
    sibling checkout) is where a silent skip does the most damage, and
    `pytest -q` never prints a skip reason.
    """
    assert hasattr(aitp, "verify_revocation_list"), (
        "installed aitp-sdk has no verify_revocation_list (needs >=0.6.0) — "
        "the verify-or-discard path is UNTESTED, not passing"
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


def _stub_transport(monkeypatch, *, body: str | None = None, status_code: int = 200,
                     raise_exc: Exception | None = None) -> None:
    """Replace `revocation_refresh`'s *own* `httpx` binding with one whose
    `AsyncClient` talks to an in-process `MockTransport`.

    This rebinds the module-global name `httpx` inside `revocation_refresh`'s
    namespace only (`monkeypatch.setattr(revocation_refresh, "httpx", ...)`).
    It does NOT touch the real `httpx` module object, which `agent_admin` and
    every other importer of `httpx` still see unmodified.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status_code, text=body)

    def async_client_factory(*args, **kwargs):
        # The real call is `httpx.AsyncClient(timeout=10.0)`; drop kwargs the
        # transport doesn't need and swap in the mock transport.
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    fake_httpx = types.SimpleNamespace(AsyncClient=async_client_factory)
    monkeypatch.setattr(revocation_refresh, "httpx", fake_httpx)


async def _apply(
    monkeypatch,
    state: RevocationState,
    envelope_json: str,
    expected_issuer: str,
    *,
    quiet: bool = False,
    events: list[dict[str, Any]] | None = None,
) -> str | None:
    """The real ingest path, with the network stubbed. NOT a reimplementation.

    `refresh_revocations` fetches over httpx, so the transport is replaced
    with a `MockTransport` that serves `envelope_json`; everything after the
    fetch — the two pre-flight guards, verification, the discard decision,
    the post-verification parse and the wholesale apply — is the production
    code in `revocation_refresh.refresh_revocations`.

    Returns the discard cause, or `None` on a successful apply — the same
    shape the old hand-rolled `_apply` returned, so the assertions below stay
    readable.
    """
    _stub_transport(monkeypatch, body=envelope_json)
    captured = events if events is not None else []

    async def _emit(event_type: str, bootstrap: dict, **fields: Any) -> None:
        captured.append({"type": event_type, **fields})

    bootstrap = {"cp": {"base_url": "http://cp.invalid", "aid": expected_issuer}}
    result = await revocation_refresh.refresh_revocations(
        revocation=state, bootstrap=bootstrap, emit=_emit, quiet=quiet,
    )
    return result.get("discarded")


async def test_a_genuine_snapshot_is_applied(monkeypatch) -> None:
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")])
    assert await _apply(monkeypatch, state, env, aid) is None
    assert state.is_revoked(_jti("jti-1"))


async def test_tampered_entries_are_discarded_and_the_deny_set_is_untouched(monkeypatch) -> None:
    """Acceptance cause 1. The deny-set must not move at all."""
    state = RevocationState()
    aid, env = _snapshot([_jti("good")])
    await _apply(monkeypatch, state, env, aid)

    forged = json.loads(env)
    forged["revocation_list"]["entries"].append({"jti": _jti("injected"), "revoked_at": 0})

    assert await _apply(monkeypatch, state, json.dumps(forged), aid) == "signature_invalid"
    assert not state.is_revoked(_jti("injected")), "a forged entry reached the deny-set"
    assert state.is_revoked(_jti("good")), "the previously verified snapshot was dropped"


async def test_a_wrapped_form_signature_is_discarded(monkeypatch) -> None:
    """Acceptance cause 2 — the pre-0.5.0 convention, rejected at ingest."""
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")], sign_wrapped=True)
    assert await _apply(monkeypatch, state, env, aid) == "signature_invalid"
    assert not state.is_revoked(_jti("jti-1"))


async def test_a_correctly_self_signed_snapshot_from_the_wrong_issuer_is_discarded(monkeypatch) -> None:
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
    assert await _apply(monkeypatch, RevocationState(), attacker_env, attacker_aid) is None

    assert await _apply(monkeypatch, state, attacker_env, pinned_aid) == "issuer_mismatch"
    assert not state.is_revoked(_jti("injected"))


async def test_an_expired_snapshot_is_discarded_and_previous_entries_stay_enforced(monkeypatch) -> None:
    """Acceptance cause 4."""
    state = RevocationState()
    key = Ed25519PrivateKey.generate()
    aid, fresh = _snapshot([_jti("still-revoked")], issuer_key=key)
    await _apply(monkeypatch, state, fresh, aid)

    _, stale = _snapshot([_jti("new")], issuer_key=key, published_at=int(time.time()) - 7200, ttl=3600)
    assert await _apply(monkeypatch, state, stale, aid) == "expired"
    assert state.is_revoked(_jti("still-revoked")), (
        "an expired snapshot discarded the previously verified one"
    )
    assert not state.is_revoked(_jti("new"))


async def test_an_unknown_version_is_discarded_with_its_own_cause(monkeypatch) -> None:
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

    assert await _apply(monkeypatch, state, json.dumps(doc), aid) == "version_unknown"
    assert not state.is_revoked(_jti("jti-1"))


async def test_malformed_input_is_discarded(monkeypatch) -> None:
    state = RevocationState()
    aid, _ = _snapshot([])
    assert await _apply(monkeypatch, state, "not json at all", aid) is not None
    assert len(state) == 0


async def test_an_empty_verified_snapshot_is_applied_not_treated_as_suspect(monkeypatch) -> None:
    """RFC-AITP-0008 §1.5: empty lists are signed, and mean something.

    A control plane whose DB read fails publishes an empty *signed* list. That
    snapshot verifies and MUST be accepted — the suppression window there is
    the CP's to own. Treating empty as suspect playground-side would be
    inventing a policy the spec does not have.
    """
    state = RevocationState()
    key = Ed25519PrivateKey.generate()
    aid, first = _snapshot([_jti("was-revoked")], issuer_key=key)
    await _apply(monkeypatch, state, first, aid)
    assert state.is_revoked(_jti("was-revoked"))

    _, empty = _snapshot([], issuer_key=key)
    assert await _apply(monkeypatch, state, empty, aid) is None
    assert not state.is_revoked(_jti("was-revoked"))
    assert state.snapshot is not None, "an empty snapshot is still a snapshot"


async def test_discarding_never_clears_a_local_revocation(monkeypatch) -> None:
    """Axis A must not touch the operator's own denials, in any failure mode."""
    state = RevocationState()
    state.revoke_local(_jti("mine"))
    aid, _ = _snapshot([])
    _, attacker_env = _snapshot([_jti("injected")])

    assert await _apply(monkeypatch, state, attacker_env, aid) == "issuer_mismatch"
    assert state.is_revoked(_jti("mine"))


# --- Pre-flight guards (previously untested; nothing imported this module) ---


async def test_no_pinned_issuer_discards_before_any_verification(monkeypatch) -> None:
    """`revocation_refresh.py:103-108` — `CP_AID` unset.

    Deleting this guard is the audit's exact example of how the old
    hand-rolled `_apply` let a real defect through unnoticed: it never called
    this code, so it could never fail this way.
    """
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")])
    events: list[dict[str, Any]] = []
    assert await _apply(monkeypatch, state, env, "", events=events) == "no_expected_issuer"
    assert not state.is_revoked(_jti("jti-1"))
    assert events == [{
        "type": "revocation.verify_failed",
        "cause": "no_expected_issuer",
        "detail": (
            "no CP AID pinned (set CP_AID) — refusing to apply an "
            "unverifiable revocation snapshot"
        ),
    }]


async def test_sdk_without_verify_surface_discards_rather_than_probing_silently(monkeypatch) -> None:
    """`revocation_refresh.py:109-114` — the SDK capability probe.

    `PENDING.md` P8 forbids a silent capability-probe downgrade; this is the
    guard that keeps that forbidden shape from reappearing.
    """
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")])
    monkeypatch.delattr(revocation_refresh.aitp, "verify_revocation_list", raising=False)
    assert await _apply(monkeypatch, state, env, aid) == "sdk_cannot_verify"
    assert not state.is_revoked(_jti("jti-1"))


async def test_transport_failure_and_verify_failure_never_alias_each_other(monkeypatch) -> None:
    """D-5's requirement, carried into the revocation path.

    A transport failure must emit `revocation.refresh_failed` with an
    `error` field and never `revocation.verify_failed`; a forged snapshot
    must emit the reverse. Collapsing the two is how a signing-convention
    break gets triaged as a network blip.
    """
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")])

    transport_events: list[dict[str, Any]] = []
    _stub_transport(monkeypatch, raise_exc=httpx.ConnectError("connection refused"))
    captured = transport_events

    async def _emit(event_type: str, bootstrap: dict, **fields: Any) -> None:
        captured.append({"type": event_type, **fields})

    result = await revocation_refresh.refresh_revocations(
        revocation=state,
        bootstrap={"cp": {"base_url": "http://cp.invalid", "aid": aid}},
        emit=_emit,
    )
    assert "error" in result and "discarded" not in result
    assert [e["type"] for e in transport_events] == ["revocation.refresh_failed"]
    assert "error" in transport_events[0]
    assert "cause" not in transport_events[0]

    forged_events: list[dict[str, Any]] = []
    forged = json.loads(env)
    forged["revocation_list"]["entries"].append({"jti": _jti("injected"), "revoked_at": 0})
    assert await _apply(monkeypatch, state, json.dumps(forged), aid, events=forged_events) == "signature_invalid"
    assert [e["type"] for e in forged_events] == ["revocation.verify_failed"]
    assert "cause" in forged_events[0]
    assert "error" not in forged_events[0]


# --- Phase 3: `revocation.verify_failed` survives a `quiet=True` poll ---


async def test_a_quiet_poll_discard_still_emits_verify_failed(monkeypatch) -> None:
    """`revocation_refresh.py:92` — `_discard` must not honour `quiet`.

    The background poll always calls with `quiet=True`
    (`aitp_server.py:223`). If a discard were suppressed there, an
    attacker-signed or wrong-issuer snapshot in steady state would be
    completely silent — indistinguishable from a healthy CP that is merely
    unreachable. See `DECISIONS.md` D-14.
    """
    state = RevocationState()
    pinned_aid, _ = _snapshot([])
    _, attacker_env = _snapshot([_jti("injected")])

    events: list[dict[str, Any]] = []
    assert await _apply(
        monkeypatch, state, attacker_env, pinned_aid, quiet=True, events=events,
    ) == "issuer_mismatch"
    assert events == [{
        "type": "revocation.verify_failed",
        "cause": "issuer_mismatch",
        "detail": events[0]["detail"],
    }]


async def test_a_quiet_poll_transport_failure_emits_refresh_failed_not_verify_failed(monkeypatch) -> None:
    """The `quiet` suppression that DOES remain: transport noise stays quiet."""
    state = RevocationState()
    aid, _ = _snapshot([])

    events: list[dict[str, Any]] = []
    _stub_transport(monkeypatch, raise_exc=httpx.ConnectError("connection refused"))

    async def _emit(event_type: str, bootstrap: dict, **fields: Any) -> None:
        events.append({"type": event_type, **fields})

    result = await revocation_refresh.refresh_revocations(
        revocation=state,
        bootstrap={"cp": {"base_url": "http://cp.invalid", "aid": aid}},
        emit=_emit,
        quiet=True,
    )
    assert "error" in result
    assert events == [], "a quiet transport failure must stay quiet — only verify_failed is exempt"


async def test_a_quiet_poll_success_stays_quiet(monkeypatch) -> None:
    """The other `quiet` suppression that must still work: a routine success
    emits no `revocation.list_fetched` — otherwise this phase quietly deleted
    the flag instead of narrowing it.
    """
    state = RevocationState()
    aid, env = _snapshot([_jti("jti-1")])
    events: list[dict[str, Any]] = []
    assert await _apply(monkeypatch, state, env, aid, quiet=True, events=events) is None
    assert events == [], "a quiet successful refresh must not emit list_fetched"


# --- Phase 5: a snapshot that verifies but has a malformed body ---


async def test_a_verified_but_malformed_body_is_discarded_not_a_500(monkeypatch) -> None:
    """`revocation_refresh.py`'s post-verification parse.

    The installed SDK's own deserialization is strict enough that it rejects
    every malformed shape tried here (missing `expires_at`, non-numeric
    timestamps, non-UUID `jti`) *before* the signature check even runs —
    `aitp.verify_revocation_list` cannot itself be made to accept a body this
    module's own parse then chokes on. So `verify_revocation_list` is
    monkeypatched to a no-op here, isolating this module's own defensive
    parse from the SDK's: "if verification passes — by whatever means, on
    whatever future SDK — a malformed body must still discard, not crash."
    Exposure in the real system requires the CP's own private key (only the
    CP can produce something that verifies), so this is a taxonomic gap, not
    a security hole — but before this fix it raised `KeyError` out of the
    function rather than reaching `_discard`, so the admin route returned a
    bare 500.
    """
    aid = "aid:pubkey:does-not-matter-verification-is-stubbed"
    envelope = json.dumps({
        "revocation_list": {
            "version": "aitp/0.2",
            "issuer": aid,
            "published_at": int(time.time()),
            # "expires_at" deliberately omitted.
            "entries": [{"jti": _jti("jti-1"), "revoked_at": int(time.time())}],
        },
        "signature": "irrelevant-verification-is-stubbed",
    })
    monkeypatch.setattr(
        revocation_refresh.aitp, "verify_revocation_list", lambda *a, **k: None
    )

    state = RevocationState()
    state.revoke_local(_jti("mine"))  # must survive a discard, like any other
    events: list[dict[str, Any]] = []
    cause = await _apply(monkeypatch, state, envelope, aid, events=events)
    assert cause == "malformed_body"
    assert not state.is_revoked(_jti("jti-1")), "the malformed snapshot must not apply"
    assert state.is_revoked(_jti("mine")), "a local revocation must survive the discard"
    assert [e["type"] for e in events] == ["revocation.verify_failed"]
    assert events[0]["cause"] == "malformed_body"
