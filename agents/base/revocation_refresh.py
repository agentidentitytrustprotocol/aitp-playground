"""The single revocation-snapshot ingest path.

This lives apart from `agent_admin` so there is exactly **one** place that
fetches, verifies and applies a snapshot — callable both from the admin route
and directly, without an HTTP hop.

That mattered more than it looked. The first version had the background poll
call the agent's own `/admin/refresh-revocations` over localhost, on the
reasoning that reusing the route kept one ingest path. It does, but it also
makes the refresh unavailable until uvicorn is accepting connections — and
under `fail_closed` an agent with no verified snapshot rejects every
capability call. Scenarios make their first call milliseconds after
agent-ready, so no poll cadence, however tight, closes that window: the
refresh has to happen *before* the agent reports ready, which means it cannot
go over the network to itself.

Two hand-rolled copies of this logic are exactly how the repo ended up with
two signature-blind parses in the first place. One function, two callers.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

import aitp
import httpx

from revocation_state import RevocationState

#: `emit_event`-shaped callable, injected so this module stays independent of
#: the telemetry transport (and so tests can capture events without a server).
EventEmitter = Callable[..., Awaitable[None]]


async def refresh_revocations(
    *,
    revocation: RevocationState,
    bootstrap: dict[str, Any],
    emit: EventEmitter,
    cp_base_url: Optional[str] = None,
    cp_api_key: Optional[str] = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Fetch, verify, and either apply or discard a revocation snapshot.

    Returns a result dict for the admin route; the caller decides what to do
    with it. `quiet` suppresses per-attempt telemetry for poll-driven calls —
    specifically `revocation.list_fetched` (a successful, routine refresh) and
    `revocation.refresh_failed` (a transport error, which at a 60s cadence
    against a down control plane is one event per tick forever). It does
    **not** suppress `revocation.verify_failed`: a discard means the CP
    answered with something that does not verify, which is not routine noise
    — see `_discard`'s docstring and `DECISIONS.md` D-14.

    `cp_base_url` / `cp_api_key` override the bootstrap values. The pinned
    issuer AID deliberately has **no** override: the URL may be chosen per
    call, but if the expected issuer could be too, an attacker-chosen endpoint
    would supply its own AID and verification would be a formality.
    """
    cp = bootstrap.get("cp") if isinstance(bootstrap.get("cp"), dict) else {}
    base_url = cp_base_url or cp.get("base_url")
    api_key = cp_api_key or cp.get("api_key")
    expected_issuer = cp.get("aid")

    if not base_url:
        return {
            "revoked_count": len(revocation),
            "added": 0,
            "skipped": "no cp configured",
        }

    url = f"{base_url.rstrip('/')}/.well-known/aitp-revocation-list"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            envelope_json = resp.text
    except Exception as exc:  # noqa: BLE001
        # Transport failure only. This must never alias a verification
        # failure: collapsing them is how a signing-convention break gets
        # triaged as a network blip.
        if not quiet:
            await emit("revocation.refresh_failed", bootstrap, error=str(exc))
        return {"revoked_count": len(revocation), "added": 0, "error": str(exc)}

    async def _discard(cause: str, detail: str) -> dict[str, Any]:
        """RFC-AITP-0008 §1.5: an unverifiable snapshot is DISCARDED.

        Not applied, not partially applied, not merged. The previously
        verified snapshot stays in force and the deny-set is untouched. This
        is a MUST, so it has no mode knob — `revocation_fail_mode` governs the
        *absence* of a fresh snapshot, never its authenticity.

        `verify_failed` is never suppressed by `quiet`, unlike every other
        event this module emits. `quiet` exists for routine poll-cadence
        noise (`refresh_failed` fires on every tick of an ordinary CP outage);
        a discard means the CP answered with something that does NOT verify
        — forged, wrong-issuer, expired, or an unverifiable SDK/config state —
        which is not routine. Applying `quiet` here previously buried exactly
        the signal the two callers' docstrings already claimed it preserved.
        See `DECISIONS.md` D-14.
        """
        await emit(
            "revocation.verify_failed", bootstrap, cause=cause, detail=detail
        )
        return {
            "revoked_count": len(revocation),
            "added": 0,
            "discarded": cause,
            "detail": detail,
        }

    if not expected_issuer:
        return await _discard(
            "no_expected_issuer",
            "no CP AID pinned (set CP_AID) — refusing to apply an "
            "unverifiable revocation snapshot",
        )
    if not hasattr(aitp, "verify_revocation_list"):
        return await _discard(
            "sdk_cannot_verify",
            "installed aitp-sdk has no verify_revocation_list (needs >=0.6.0) "
            "— refusing to apply an unverified snapshot",
        )

    try:
        aitp.verify_revocation_list(envelope_json, expected_issuer)
    except Exception as exc:  # noqa: BLE001
        return await _discard(getattr(exc, "code", None) or "signature_invalid", str(exc))

    # Verified. Parse the exact RFC-AITP-0008 §1.5 envelope — no tolerant
    # fallback. Once the signature is checked, accepting a second shape is a
    # downgrade path: a body the parser accepts but the verifier did not sign
    # over. A snapshot that VERIFIES (so it was signed by the pinned CP key)
    # but whose body is malformed still goes through `_discard` rather than
    # raising — every other discard cause reaches the admin route as a result
    # dict, and this one needs to as well, not a bare 500. Exposure to this
    # path requires the CP's own private key, so it is a taxonomic gap, not a
    # security one.
    try:
        body_obj = json.loads(envelope_json)["revocation_list"]
        jtis = [
            e["jti"]
            for e in body_obj.get("entries", [])
            if isinstance(e, dict) and isinstance(e.get("jti"), str)
        ]
        published_at = int(body_obj["published_at"])
        expires_at = int(body_obj["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        return await _discard("malformed_body", str(exc))

    previous = revocation.snapshot_entry_count
    # Wholesale replacement, not a merge — a snapshot is the issuer's complete
    # current deny-set, so a jti it no longer lists is no longer revoked.
    revocation.apply_snapshot(jtis, published_at=published_at, expires_at=expires_at)

    if not quiet:
        await emit(
            "revocation.list_fetched",
            bootstrap,
            jti_count=len(jtis),
            added=max(0, len(jtis) - previous),
            verified=True,
            issuer=expected_issuer,
        )
    return {
        "revoked_count": len(revocation),
        "snapshot_count": len(jtis),
        "added": max(0, len(jtis) - previous),
        "verified": True,
    }
