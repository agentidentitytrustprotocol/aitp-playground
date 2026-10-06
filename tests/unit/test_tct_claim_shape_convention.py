"""TCT claim-shape interlock against a minter that is not the AITP wheel (PENDING.md P16).

Every other TCT in this suite is minted and verified by the same `aitp-sdk`
wheel, so a drift in the closed claim set (`ver jti iss sub aud iat exp grants
cnf ext`) would pass unseen -- the same blind spot D-1 closed for revocation
signing and the pinned-key proof. Here the compact JWS is signed with
`cryptography` (pyca) directly, from the claims and keys the spec pins in
`schemas/conformance/tct-011` / `tct-012` and `known-answer/keypairs.json`
(issuer = kat-keypair-002), and then verified by the wheel.

Claims are inlined rather than read from the spec checkout so the test does not
depend on a sibling repository.
"""

from __future__ import annotations

import base64
import json

import pytest

aitp = pytest.importorskip("aitp")
ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

ISSUER_SEED_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
GRANT = "macp.mode.task.v1"

# tct-012's claims, minus the `ext` member (the plain, fully-defined shape).
BASE_CLAIMS = {
    "ver": "aitp/0.2",
    "jti": "550e8400-e29b-41d4-a716-446655443012",
    "iss": "aid:pubkey:A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg",
    "sub": "aid:pubkey:O2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik",
    "aud": "aid:pubkey:O2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik",
    "iat": 1711800000,
    "exp": 9999999999,
    "grants": [GRANT],
    "cnf": {"jkt": "9ZP03Nu8GrXPAUkbKNxHOKBzxPX83SShgFkRNK-f2lw"},
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _mint(claims: dict) -> str:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(ISSUER_SEED_HEX))
    header = _b64(json.dumps({"alg": "EdDSA", "typ": "aitp-tct+jwt"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{header}.{payload}.{_b64(key.sign(f'{header}.{payload}'.encode()))}"


@pytest.fixture(scope="module")
def issuer():
    agent = aitp.AitpAgent.from_seed(bytes.fromhex(ISSUER_SEED_HEX))
    assert agent.aid == BASE_CLAIMS["iss"], "kat-keypair-002 no longer maps to the fixture issuer"
    return agent


def _verify(issuer, claims: dict):
    return issuer.verify_tct(_mint(claims), GRANT, claims["aud"])


def test_independently_minted_tct_verifies(issuer):
    assert _verify(issuer, BASE_CLAIMS).jti == BASE_CLAIMS["jti"]


def test_ext_member_is_accepted_tct_012(issuer):
    claims = {**BASE_CLAIMS, "ext": {"com.example.debug_trace": {"session": "diag-4471"}}}
    assert _verify(issuer, claims).jti == BASE_CLAIMS["jti"]


def test_unknown_sibling_claim_is_rejected_tct_011(issuer):
    claims = {**BASE_CLAIMS, "jti": "550e8400-e29b-41d4-a716-446655443011", "device_id": "phone-77"}
    with pytest.raises(ValueError, match="unknown field `device_id`"):
        _verify(issuer, claims)
