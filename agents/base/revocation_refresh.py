"""The single revocation ingest path: put this agent's own list into force.

RFC-AITP-0008 §1.5 makes a list valid only from the issuer of the tokens it
names, and an agent verifies the TCTs it issued itself (a capability call
presents a TCT this agent minted at the handshake; a delegation's source TCT
was minted by the redeemer). So the deny-set this agent enforces as a
verifier is its **own** signed list, the one it serves at
`/.well-known/aitp-revocation-list`, plus its local revocations. The control
plane only relays entries; it signs nothing (CP1.1), so there is no CP key to
pin and no `CP_AID`.

One refresh is: pull this issuer's entries from the CP and re-sign the list
(`IssuerRevocationList.refresh`), then verify the list in force through the
SDK against this agent's own AID and apply its entries. The verification is
the same `aitp.verify_revocation_list` call any peer makes, so the deny-set
is never built from bytes the SDK has not accepted.

This lives apart from `agent_admin` so there is exactly **one** place that
does this, callable from the admin route, the start-up refresh and the poll
without an HTTP hop. The start-up refresh runs before `AITP_AGENT_READY`:
under `fail_closed` an agent with no list in force rejects every capability
call, and a scenario's first call lands milliseconds after ready.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import aitp

from revocation_publish import IssuerRevocationList
from revocation_state import RevocationState

#: `emit_event`-shaped callable, injected so this module stays independent of
#: the telemetry transport (and so tests can capture events without a server).
EventEmitter = Callable[..., Awaitable[None]]


async def refresh_revocations(
    *,
    revocation: RevocationState,
    issued: IssuerRevocationList,
    bootstrap: dict[str, Any],
    emit: EventEmitter,
    quiet: bool = False,
) -> dict[str, Any]:
    """Re-pull, re-sign, verify and apply this agent's own revocation list.

    Returns a result dict for the admin route; the caller decides what to do
    with it. `quiet` suppresses routine per-attempt telemetry for poll-driven
    calls (`revocation.list_fetched`, and the publish events `issued.refresh`
    rate-limits itself). It does **not** suppress `revocation.verify_failed`:
    a list in force that does not verify against our own AID is a defect,
    not routine noise — see `DECISIONS.md` D-14.

    A failed pull leaves the last list in force until its signed
    `expires_at`, which is what keeps a CP outage from un-revoking anything
    (and from revoking everything before the list actually lapses).
    """
    if issued.cp_base_url is None:
        return {
            "revoked_count": len(revocation),
            "added": 0,
            "skipped": "no cp configured",
        }

    pulled = await issued.refresh(emit=emit, quiet=quiet)
    current = issued.serveable()
    if current is None:
        # Never pulled, or the last list expired. Nothing to apply: the
        # posture reads the (absent or expired) snapshot and fails closed.
        return {
            "revoked_count": len(revocation),
            "added": 0,
            "published": False,
            "error": pulled.get("detail") or pulled.get("cause"),
        }

    expected_issuer = current.signer_aid

    async def _discard(cause: str, detail: str) -> dict[str, Any]:
        """RFC-AITP-0008 §1.5: an unverifiable list is DISCARDED.

        Not applied, not partially applied, not merged. The previously
        applied list stays in force and the deny-set is untouched. This is a
        MUST, so it has no mode knob — `revocation_fail_mode` governs the
        *absence* of a fresh list, never its authenticity.
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

    try:
        aitp.verify_revocation_list(current.envelope_json, expected_issuer)
    except Exception as exc:  # noqa: BLE001
        return await _discard(getattr(exc, "code", None) or "signature_invalid", str(exc))

    # Verified. Parse the exact RFC-AITP-0008 §1.5 envelope — no tolerant
    # fallback: once the signature is checked, accepting a second shape is a
    # body the parser accepts but the verifier did not sign over.
    try:
        body_obj = json.loads(current.envelope_json)["revocation_list"]
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
    # Wholesale replacement, not a merge — the list is the issuer's complete
    # current deny-set, so a jti it no longer names is no longer revoked.
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
        "published": bool(pulled.get("published")),
    }
