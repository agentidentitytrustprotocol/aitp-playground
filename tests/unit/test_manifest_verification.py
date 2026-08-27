"""Peer manifests are verified before any field is read out of them.

Both sites in this module's scope that ingest a *peer* `ManifestEnvelope`
used to parse it signature-blind (the runner's CP trust-anchor fetch is a
third site, covered in `test_engine_run.py`):

* `/admin/initiate-handshake` — takes `aid` and `handshake_endpoint` from the
  fetched envelope and hands them to the handshake.
* `/admin/delegate` — takes `aid` and mints a delegation **to it**. A
  peer that can answer at the delegatee's manifest URL substituted its own AID
  and received the delegation, scope and all.

`aitp.verify_manifest_json` was already bound in the SDK the whole time; it was
simply never called. These tests pin that it now is, and that a manifest which
fails verification stops the request rather than falling through to an
unverified AID.

Note the placement asymmetry, which is deliberate in the protocol and easy to
"fix" wrongly: a manifest carries `signature` **inside** the body (stripped
before canonicalizing), while a revocation snapshot carries it as a **sibling**
of the body. See `test_revocation_signing_convention.py` and
RFC-AITP-0001 §5.4.1.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import aitp

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

from agent_admin import _verify_peer_manifest  # noqa: E402

# No telemetry_url, so emit_event logs at debug and makes no network call
# (agents/base/telemetry.py:22). The tests below assert on the emitted
# event by patching emit_event, not by standing up a collector.
_BOOTSTRAP = {"run_id": "test-run", "agent_id": "peer"}
from fastapi import HTTPException  # noqa: E402


def _mint(agent: "aitp.AitpAgent", *, endpoint: str = "http://localhost:9/aitp/handshake/hello",
          ttl_secs: int | None = None) -> str:
    kwargs = {"display_name": "peer", "handshake_endpoint": endpoint, "offered_caps": ["demo.x"]}
    if ttl_secs is not None:
        kwargs["ttl_secs"] = ttl_secs
    return agent.build_manifest(**kwargs)


@pytest.fixture
def peer() -> "aitp.AitpAgent":
    return aitp.AitpAgent.generate()


async def test_a_genuine_manifest_verifies_and_yields_its_body(peer: "aitp.AitpAgent") -> None:
    manifest = await _verify_peer_manifest(
        _mint(peer), "http://peer/.well-known/aitp-manifest", _BOOTSTRAP
    )
    assert manifest["aid"] == peer.aid
    # The fields the two call sites actually consume.
    assert manifest["handshake_endpoint"]
    assert "offered_capabilities" in manifest


async def test_a_tampered_body_is_rejected(peer: "aitp.AitpAgent") -> None:
    """Non-vacuity: mint a real envelope, change one byte of the signed body."""
    envelope = json.loads(_mint(peer))
    envelope["manifest"]["display_name"] = "evil"
    with pytest.raises(HTTPException) as exc:
        await _verify_peer_manifest(json.dumps(envelope), "http://peer/m", _BOOTSTRAP)
    assert exc.value.status_code == 502
    assert "failed verification" in exc.value.detail


async def test_a_substituted_aid_is_rejected(peer: "aitp.AitpAgent") -> None:
    """The attack the delegation site was open to.

    Swapping `aid` for an attacker's own breaks the self-certifying binding
    between the identifier and the key that signed the manifest, so the
    envelope no longer verifies. This is the assertion that says the
    delegation cannot be steered to a substituted recipient.
    """
    attacker = aitp.AitpAgent.generate()
    envelope = json.loads(_mint(peer))
    envelope["manifest"]["aid"] = attacker.aid
    with pytest.raises(HTTPException) as exc:
        await _verify_peer_manifest(json.dumps(envelope), "http://peer/m", _BOOTSTRAP)
    assert exc.value.status_code == 502


async def test_a_wholesale_attacker_manifest_is_not_confused_for_the_peer(
    peer: "aitp.AitpAgent",
) -> None:
    """An attacker's *own* well-formed manifest verifies — and must.

    Verification is self-certifying: it proves who minted the envelope, not
    that they are who you wanted. This test pins the honest boundary, so the
    docstring's caveat is not just prose: the caller still has to compare the
    returned `aid` against what it expected. Phase 6's expected-issuer pin is
    the same lesson for revocation.
    """
    attacker = aitp.AitpAgent.generate()
    manifest = await _verify_peer_manifest(_mint(attacker), "http://attacker/m", _BOOTSTRAP)
    assert manifest["aid"] == attacker.aid
    assert manifest["aid"] != peer.aid


async def test_a_missing_manifest_body_is_rejected_not_crashed(peer: "aitp.AitpAgent") -> None:
    envelope = json.loads(_mint(peer))
    signature = envelope["manifest"]["signature"]
    with pytest.raises(HTTPException) as exc:
        await _verify_peer_manifest(
            json.dumps({"signature": signature}), "http://peer/m", _BOOTSTRAP
        )
    assert exc.value.status_code == 502


async def test_garbage_is_rejected_not_crashed() -> None:
    with pytest.raises(HTTPException) as exc:
        await _verify_peer_manifest("not json at all", "http://peer/m", _BOOTSTRAP)
    assert exc.value.status_code == 502


async def test_an_expired_peer_manifest_is_rejected_with_its_own_cause(
    peer: "aitp.AitpAgent", monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is a *new* failure mode this phase introduces, and it must not
    read as a signature problem.

    `verify_manifest_json` checks `expires_at` against the wall clock with no
    override (`bindings/aitp-py/src/manifest.rs`), so a peer whose manifest has
    aged out is now rejected where it previously sailed through. The `cause`
    field is what tells an operator "that peer's manifest went stale" rather
    than "someone tampered with it".
    """
    import agent_admin

    events: list[tuple[str, dict]] = []

    async def _capture(event_type, _bootstrap, **fields):
        events.append((event_type, fields))

    monkeypatch.setattr(agent_admin, "emit_event", _capture)

    # A negative TTL back-dates `expires_at` one second before `published_at`,
    # so the manifest is expired the instant it is minted. Deterministic, and
    # no sleep: the SDK reads the *system* clock inside Rust
    # (`Timestamp::now()`), so monkeypatching Python's `time.time` cannot move
    # it. `ttl_secs=0` is NOT enough — `expires_at == now` is not in the past.
    envelope_json = _mint(peer, ttl_secs=-1)
    body = json.loads(envelope_json)["manifest"]
    assert int(body["expires_at"]) < int(body["published_at"])

    with pytest.raises(HTTPException) as exc:
        await _verify_peer_manifest(envelope_json, "http://peer/m", _BOOTSTRAP)

    assert exc.value.status_code == 502
    assert events and events[0][0] == "manifest.verify_failed"
    assert events[0][1]["cause"] == "expired", (
        f"an aged-out manifest must report cause=expired, got {events[0][1]}"
    )


async def test_verification_failure_emits_a_named_event_distinct_from_expiry(
    peer: "aitp.AitpAgent", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged manifest and a stale one must not look the same in telemetry.

    Collapsing them is how a signing-convention break gets triaged as a
    network blip — the confusion this whole effort exists to prevent.
    """
    import agent_admin

    events: list[tuple[str, dict]] = []

    async def _capture(event_type, _bootstrap, **fields):
        events.append((event_type, fields))

    monkeypatch.setattr(agent_admin, "emit_event", _capture)

    envelope = json.loads(_mint(peer))
    envelope["manifest"]["display_name"] = "evil"
    with pytest.raises(HTTPException):
        await _verify_peer_manifest(json.dumps(envelope), "http://peer/m", _BOOTSTRAP)

    assert events[0][0] == "manifest.verify_failed"
    assert events[0][1]["cause"] == "signature_invalid"
    assert events[0][1]["source_url"] == "http://peer/m"


async def test_malformed_input_is_its_own_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_admin

    events: list[tuple[str, dict]] = []

    async def _capture(event_type, _bootstrap, **fields):
        events.append((event_type, fields))

    monkeypatch.setattr(agent_admin, "emit_event", _capture)
    with pytest.raises(HTTPException):
        await _verify_peer_manifest("not json at all", "http://peer/m", _BOOTSTRAP)
    assert events[0][1]["cause"] in {"signature_invalid", "malformed"}


async def test_the_cause_comes_from_the_code_not_the_message_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The P4 drift guard: `.code` is the contract, prose is not.

    This previously read `"expired" in message.lower()`, which pinned the
    SDK's wording as an expected value — the same bug class the 0.5.0
    signing-input change exposed. A reworded upstream message would have
    silently reclassified a stale manifest as a forged one, and the alert
    that matters is the forged one.

    So the error raised here says nothing about expiry in its text and
    carries `code="expired"`. Under the old substring match this would have
    been reported as `signature_invalid`.
    """
    import agent_admin

    events: list[tuple[str, dict]] = []

    async def _capture(event_type, _bootstrap, **fields):
        events.append((event_type, fields))

    class _Reworded(RuntimeError):
        code = "expired"

    def _raise(_envelope_json):
        raise _Reworded("manifest verification failed: validity window elapsed")

    monkeypatch.setattr(agent_admin, "emit_event", _capture)
    monkeypatch.setattr(agent_admin.aitp, "verify_manifest_json", _raise)

    with pytest.raises(HTTPException):
        await _verify_peer_manifest("{}", "http://peer/m", _BOOTSTRAP)

    assert events[0][1]["cause"] == "expired", (
        "the cause was derived from the message text, not from `.code` — a "
        f"reworded SDK message silently reclassified it: {events[0][1]}"
    )


async def test_an_untyped_non_value_error_is_unknown_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence of a code is reported honestly, never as an attack.

    Defaulting an unrecognised failure to `signature_invalid` would page
    someone about forgery every time an unrelated bug surfaced here.
    """
    import agent_admin

    events: list[tuple[str, dict]] = []

    async def _capture(event_type, _bootstrap, **fields):
        events.append((event_type, fields))

    def _raise(_envelope_json):
        raise RuntimeError("something else went wrong entirely")

    monkeypatch.setattr(agent_admin, "emit_event", _capture)
    monkeypatch.setattr(agent_admin.aitp, "verify_manifest_json", _raise)

    with pytest.raises(HTTPException):
        await _verify_peer_manifest("{}", "http://peer/m", _BOOTSTRAP)

    assert events[0][1]["cause"] == "unknown"


def test_signature_is_a_body_member_for_manifests_unlike_revocation(
    peer: "aitp.AitpAgent",
) -> None:
    """Pins the placement asymmetry so nobody "harmonises" the two artifacts.

    RFC-AITP-0001 §5.4.1 gives the manifest member placement and the
    revocation snapshot sibling placement, for different reasons. A change
    that made these agree would break one of them.
    """
    envelope = json.loads(_mint(peer))
    assert set(envelope) == {"manifest"}, "manifest envelope has no sibling keys"
    assert "signature" in envelope["manifest"], "manifest signature is a body member"


# ── The serving side ─────────────────────────────────────────────────────
#
# Verification cuts both ways: once peers verify, this agent's own served
# manifest has to stay verifiable. It previously did not — it was minted once
# at construction and served verbatim for the life of the process, so an agent
# alive longer than its `ttl_secs` served a manifest every verifying peer would
# reject. Nothing caught it because nothing verified.


def _server(ttl_secs: int = 3600):
    from aitp_server import AitpServer  # noqa: E402 — after the sys.path insert

    agent = aitp.AitpAgent.generate()
    bootstrap = {
        "run_id": "test-run",
        "agent_id": "peer",
        "aitp": {
            "seed_hex": "11" * 32,
            "display_name": "peer",
            "handshake_endpoint": "http://localhost:9/aitp/handshake/hello",
            "offered_caps": ["demo.x"],
            "ttl_secs": ttl_secs,
        },
    }
    manifest_json = agent.build_manifest(
        display_name="peer",
        handshake_endpoint="http://localhost:9/aitp/handshake/hello",
        offered_caps=["demo.x"],
        ttl_secs=ttl_secs,
    )
    return AitpServer(
        agent=agent, manifest_json=manifest_json, port=9, bootstrap=bootstrap
    )


def _age_manifest(server, seconds: int) -> None:
    """Back-date the served manifest so its half-life has notionally passed.

    The re-mint deadline is read from the manifest's own `published_at` /
    `expires_at` rather than a field on the server, so the clock is driven by
    ageing the artifact — which is also what a real elapsed hour would do.
    """
    doc = json.loads(server.manifest_json)
    doc["manifest"]["published_at"] = int(doc["manifest"]["published_at"]) - seconds
    doc["manifest"]["expires_at"] = int(doc["manifest"]["expires_at"]) - seconds
    server.manifest_json = json.dumps(doc)


def test_served_manifest_is_stable_while_fresh() -> None:
    """No churn on the hot path — a new signature per request would be waste."""
    server = _server()
    assert server._fresh_manifest_json() == server._fresh_manifest_json()


def test_served_manifest_is_reminted_past_half_life_and_still_verifies() -> None:
    """The regression this closes: serving a credential past its own lifetime.

    The clock is driven rather than slept: rewind the mint timestamp so the
    half-life has notionally elapsed.
    """
    server = _server(ttl_secs=3600)
    before = server._fresh_manifest_json()

    _age_manifest(server, 1801)  # just past ttl/2
    after = server._fresh_manifest_json()

    assert after != before, "manifest was not re-minted past its half-life"
    assert aitp.verify_manifest_json(after) is None, "re-minted manifest must verify"

    old_body = json.loads(before)["manifest"]
    new_body = json.loads(after)["manifest"]
    assert new_body["aid"] == old_body["aid"], (
        "re-minting must keep the AID — the key did not change, only the "
        "signed validity window. A moving AID would break every peer that "
        "pinned it."
    )
    # Not `>`: the re-mint lands in the same wall-clock second as the original
    # in a fast test, so `expires_at` need not advance. The property that
    # actually matters is that what we serve always has meaningful life left —
    # which is what a verifying peer checks.
    assert int(new_body["expires_at"]) >= int(old_body["expires_at"])
    assert int(new_body["expires_at"]) - time.time() > 1800, (
        "a re-minted manifest must carry at least a half-TTL of validity; "
        "otherwise re-minting has not actually bought the peer any headroom"
    )


def test_remint_failure_serves_the_previous_manifest_rather_than_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degradation path. The old manifest is still valid for another half-TTL,
    so serving it beats serving an error — and the next request retries."""
    import aitp_server as aitp_server_mod

    server = _server(ttl_secs=3600)
    server._fresh_manifest_json()
    _age_manifest(server, 1801)
    # Captured AFTER ageing: the aged bytes are what "the previous manifest"
    # means at the moment the re-mint is attempted.
    previous = server.manifest_json

    calls = 0

    def _boom(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("signing unavailable")

    monkeypatch.setattr(aitp_server_mod, "get_manifest_json", _boom)
    assert server._fresh_manifest_json() == previous

    # And the cooldown holds: a second request must NOT retry the signature.
    # Without it every subsequent request retakes the lock, re-signs and logs
    # a full traceback — a self-inflicted stall the moment signing moves
    # behind a KMS.
    assert server._fresh_manifest_json() == previous
    assert calls == 1, f"re-mint retried during the cooldown ({calls} attempts)"

    # Past the cooldown it tries again rather than giving up for good.
    server._manifest_remint_cooldown_until = 0.0
    assert server._fresh_manifest_json() == previous
    assert calls == 2


def test_a_rotation_during_a_remint_does_not_resurrect_the_old_key() -> None:
    """The race between /admin/rotate-keys and a half-life re-mint.

    `get_manifest` is a sync route (threadpool) while `rotate_keys` is async
    (event loop), so they genuinely interleave. Without the guard, a re-mint
    that began before a rotation could finish after it and overwrite the
    new-key manifest with one signed by a key the agent no longer holds —
    served for up to half a TTL, failing every handshake against it.
    """
    server = _server(ttl_secs=3600)
    _age_manifest(server, 1801)

    rotated_agent = aitp.AitpAgent.generate()
    rotated_manifest = rotated_agent.build_manifest(
        display_name="peer",
        handshake_endpoint="http://localhost:9/aitp/handshake/hello",
        offered_caps=["demo.x"],
    )

    import aitp_server as aitp_server_mod

    original = aitp_server_mod.get_manifest_json

    def _rotate_midway(agent, bootstrap):
        # Signing is in flight; the rotation lands right now.
        minted = original(agent, bootstrap)
        server.agent = rotated_agent
        server.manifest_json = rotated_manifest
        # the rotation's manifest is fresh by construction
        return minted

    aitp_server_mod.get_manifest_json = _rotate_midway
    try:
        served = server._fresh_manifest_json()
    finally:
        aitp_server_mod.get_manifest_json = original

    assert served == rotated_manifest, (
        "the re-mint overwrote a rotated manifest with one signed by the "
        "old key"
    )
    assert json.loads(served)["manifest"]["aid"] == rotated_agent.aid
