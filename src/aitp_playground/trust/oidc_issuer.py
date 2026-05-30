"""In-process OIDC issuer config for OIDC-identity playground scenarios.

A real deployment would point at an external IdP (dex, Okta, Auth0). For
the playground we generate an Ed25519 keypair per-run and share both the
private seed (so OIDC-typed agents can mint JWTs) and the public JWK (so
peer verifiers' `JwksProvider` resolves the same issuer URL to the
matching key). All AITP protocol logic — JWT verification, manifest
binding, `cnf.jkt` checks — lives in the SDK.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


@dataclass
class RunOidcIssuer:
    """Per-run mock issuer. Generated once when the run has any OIDC agent."""

    issuer_url: str
    private_seed_b64: str
    public_jwk: dict
    kid: str

    @classmethod
    def generate(cls, *, issuer_url: str = "https://idp.aitp-playground.local/") -> "RunOidcIssuer":
        seed = os.urandom(32)
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        pub = priv.public_key()
        pub_bytes = pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        # Stable kid derived from the public key — survives serialization.
        kid = "kid-" + _b64u(hashlib.sha256(pub_bytes).digest())[:16]
        jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _b64u(pub_bytes),
            "kid": kid,
            "alg": "EdDSA",
            "use": "sig",
        }
        return cls(
            issuer_url=issuer_url,
            private_seed_b64=_b64u(seed),
            public_jwk=jwk,
            kid=kid,
        )


def mint_jwt(
    *,
    private_seed_b64: str,
    kid: str,
    issuer_url: str,
    subject: str,
    audience: str,
    nonce: str,
    cnf_jkt: str,
    now_unix_secs: int,
    ttl_secs: int = 3600,
) -> str:
    """Sign a compact JWT per RFC-AITP-0002 §2.2.

    The token carries the handshake-generated ``nonce`` and the
    ``cnf.jkt`` JWK thumbprint of the subject agent's pubkey so the
    SDK's `verify_oidc` can bind the token to the AITP identity. We
    do this with cryptography directly (rather than pyjwt) so the
    JOSE header field order is stable and matches what the SDK's
    verifier expects.
    """
    seed = base64.urlsafe_b64decode(private_seed_b64 + "==")
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    claims = {
        "iss": issuer_url,
        "sub": subject,
        "aud": audience,
        "iat": now_unix_secs,
        "exp": now_unix_secs + ttl_secs,
        "nonce": nonce,
        "cnf": {"jkt": cnf_jkt},
    }
    h_b64 = _b64u(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{h_b64}.{p_b64}".encode()
    sig = priv.sign(signing_input)
    return f"{h_b64}.{p_b64}.{_b64u(sig)}"
