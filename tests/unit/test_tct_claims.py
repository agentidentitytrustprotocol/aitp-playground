"""Unit tests for the shared compact-JWS claims reader (agents/base/tct_claims).

This helper backs every v0.2 observability path in the agent workers: it reads
JWT claims out of an opaque TCT / voucher / delegation token so the workers can
populate Control Plane event payloads and return values. It performs no
signature check (the SDK owns verification) — these tests pin the decode and
its failure modes.
"""
from __future__ import annotations

import base64
import json

import pytest

import tct_claims


def _compact_jws(claims: dict) -> str:
    """Encode a minimal three-segment compact JWS carrying ``claims`` as its
    payload. Header/signature are placeholders — the reader never inspects
    them."""
    def seg(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = seg(json.dumps({"alg": "EdDSA", "typ": "aitp-tct+jwt"}).encode())
    payload = seg(json.dumps(claims).encode())
    return f"{header}.{payload}.{seg(b'not-a-real-signature')}"


def test_decode_claims_round_trips_jwt_claim_names() -> None:
    claims = {
        "ver": "aitp/0.2",
        "iss": "aid:pubkey:issuer",
        "sub": "aid:pubkey:subject",
        "aud": "aid:pubkey:subject",
        "exp": 1781392215,
        "jti": "2d94ef45-2211-4997-a926-3b9cde854998",
        "grants": ["write.content"],
    }
    decoded = tct_claims.decode_claims(_compact_jws(claims))
    assert decoded == claims


def test_decode_claims_handles_unpadded_base64url() -> None:
    # Payload lengths that need 1/2/3 bytes of base64 padding must all decode.
    for filler in ("a", "ab", "abc"):
        token = _compact_jws({"sub": filler})
        assert tct_claims.decode_claims(token)["sub"] == filler


@pytest.mark.parametrize("bad", ["", "onlyone", "two.segments", "a.b.c.d"])
def test_decode_claims_rejects_non_compact_jws(bad: str) -> None:
    with pytest.raises(ValueError):
        tct_claims.decode_claims(bad)


def test_decode_claims_rejects_non_object_payload() -> None:
    payload = base64.urlsafe_b64encode(b"[1, 2, 3]").rstrip(b"=").decode()
    with pytest.raises(ValueError):
        tct_claims.decode_claims(f"h.{payload}.s")


def test_tct_event_wraps_token_and_claims() -> None:
    claims = {"iss": "aid:pubkey:i", "sub": "aid:pubkey:s", "jti": "x"}
    token = _compact_jws(claims)
    ev = tct_claims.tct_event(token)
    assert ev == {"token": token, "claims": claims}


def test_tct_event_is_lossless_on_undecodable_token() -> None:
    # Never drop the event: an undecodable token still ships, with empty claims
    # so the Control Plane records at least the opaque artifact.
    ev = tct_claims.tct_event("not-a-jws")
    assert ev == {"token": "not-a-jws", "claims": {}}
