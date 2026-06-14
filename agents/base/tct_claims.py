"""Read claims out of a compact-JWS trust artifact for OBSERVABILITY only.

Under AITP v0.2 a TCT / grant voucher / delegation token is an opaque
RFC 7515 compact-JWS string (``header.payload.signature``). The aitp-py SDK
returns a deliberately minimal verified view (``TctIdentity`` exposes only
``peer_aid``/``grants``/``expires_at``/``jti``); it does not surface the full
claim set (``iss``/``sub``/``aud``/``iat``/``cnf`` …).

The playground needs those extra claims to (a) populate Control Plane event
payloads (``payload.tct = {token, claims}``) and (b) drive the existing
narrator/return-value fields. This helper base64url-decodes the JWS payload
segment to read them.

This is NOT AITP protocol logic and NOT a trust gate. It performs no signature
check and its output must never be used for an authorization decision — every
security decision still goes through the SDK's ``verify_tct`` /
``verify_delegation`` calls, which verify the signature. We only read
already-issued, display-level values. The pattern mirrors the SDK's own Python
test suite (``aitp-py/tests/test_renewal.py``), which decodes the payload the
same way to inspect claims.
"""
from __future__ import annotations

import base64
import json
from typing import Any


def decode_claims(token: str) -> dict[str, Any]:
    """Return the (unverified) JWT claims dict from a compact-JWS ``token``.

    Raises ``ValueError`` if ``token`` is not a three-segment compact JWS or
    its payload is not valid base64url-encoded JSON. Callers building
    best-effort observability payloads should treat any failure as "claims
    unavailable" rather than propagating it.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a compact JWS: {len(parts)} segment(s)")
    payload_seg = parts[1]
    padded = payload_seg + "=" * (-len(payload_seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"undecodable JWS payload: {exc}") from exc
    if not isinstance(claims, dict):
        raise ValueError("JWS payload is not a JSON object")
    return claims


def tct_event(token: str) -> dict[str, Any]:
    """Build the ``{token, claims}`` Control Plane event shape for a TCT.

    The CP records ``claims`` verbatim and never parses ``token`` (it is a
    trustless observer). On a decode failure we still emit the opaque token
    with empty claims so the event is never dropped.
    """
    try:
        claims = decode_claims(token)
    except ValueError:
        claims = {}
    return {"token": token, "claims": claims}
