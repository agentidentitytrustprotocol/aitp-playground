"""Agent-side OIDC helpers.

The playground engine generates a per-run mock OIDC issuer keypair and
distributes it to each agent via bootstrap. OIDC-typed agents need to
mint short-lived JWTs during their handshakes; the SDK verifies those
JWTs via the `JwksProvider` (which is preloaded here with the run's
public JWK).

No protocol logic lives here — JWT verification, manifest binding, and
`cnf.jkt` checks are all in the SDK. This module only signs the
RFC-AITP-0002 §2.2 token shape using the issuer key the playground
generated.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable, Optional

import aitp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class OidcContext:
    """Per-agent OIDC state derived from the bootstrap ``oidc`` block."""

    def __init__(self, bootstrap: dict[str, Any]) -> None:
        oidc_cfg = bootstrap.get("oidc") or {}
        aitp_cfg = bootstrap.get("aitp") or {}
        self.enabled = bool(oidc_cfg)
        self.issuer_url: Optional[str] = oidc_cfg.get("issuer_url")
        self.kid: Optional[str] = oidc_cfg.get("kid")
        self.public_jwk: Optional[dict] = oidc_cfg.get("public_jwk")
        self.private_seed_b64: Optional[str] = oidc_cfg.get("private_seed_b64")
        self.identity_type: str = aitp_cfg.get("identity_type", "pinned_key")
        self.subject: Optional[str] = aitp_cfg.get("oidc_subject")
        self.jwks: Optional["aitp.JwksProvider"] = None
        if self.enabled and self.issuer_url and self.public_jwk:
            # Every agent — even pinned-key ones — gets the verifier
            # provider so it can accept OIDC peers.
            self.jwks = aitp.JwksProvider({self.issuer_url: [self.public_jwk]})

    @property
    def trust_anchors(self) -> Optional[list[str]]:
        if self.enabled and self.issuer_url:
            return [self.issuer_url]
        return None

    def mint_jwt_for(
        self, *, audience: str, agent: "aitp.AitpAgent", ttl_secs: int = 3600,
    ) -> Optional[Callable[[str], str]]:
        """Return a `(nonce) -> str` closure for use as ``oidc_mint_jwt``.

        Returns ``None`` when this agent is not OIDC-typed — the SDK
        only requires the callback when the agent's manifest declares
        OIDC identity.
        """
        if self.identity_type != "oidc" or not self.private_seed_b64:
            return None
        seed = _b64u_decode(self.private_seed_b64)
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        kid = self.kid or ""
        issuer = self.issuer_url or ""
        sub = self.subject or ""
        cnf_jkt = aitp.compute_aid_jkt(agent.aid)

        def _mint(nonce: str) -> str:
            now = int(time.time())
            header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
            claims = {
                "iss": issuer,
                "sub": sub,
                "aud": audience,
                "iat": now,
                "exp": now + ttl_secs,
                "nonce": nonce,
                "cnf": {"jkt": cnf_jkt},
            }
            h_b64 = _b64u(json.dumps(header, separators=(",", ":")).encode())
            p_b64 = _b64u(json.dumps(claims, separators=(",", ":")).encode())
            sig = priv.sign(f"{h_b64}.{p_b64}".encode())
            return f"{h_b64}.{p_b64}.{_b64u(sig)}"

        return _mint


def peer_aid_from_hello_envelope(hello_json: str) -> Optional[str]:
    """Best-effort peer-AID extraction from a MUTUAL_HELLO envelope.

    Used by the responder to set ``aud`` correctly when minting its
    own JWT in process_hello. Returns ``None`` if the envelope shape
    is unexpected — the caller should fall back to a no-op mint and
    let the SDK reject malformed payloads.
    """
    try:
        env = json.loads(hello_json)
    except json.JSONDecodeError:
        return None
    payload = env.get("payload")
    if isinstance(payload, dict):
        manifest = payload.get("manifest") or {}
        aid = manifest.get("aid")
        if isinstance(aid, str):
            return aid
    sender = env.get("sender")
    return sender if isinstance(sender, str) else None
