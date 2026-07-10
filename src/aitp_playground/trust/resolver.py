"""Peer resolver utilities — pure HTTP/DID lookups, no AITP crypto."""
from __future__ import annotations

import os
from urllib.parse import quote, unquote

import httpx


def _http_allowed(host: str) -> bool:
    """Whether ``host`` may be resolved over plain http rather than https.

    Loopback is always http. Beyond that, a test-only allowlist
    (``AITP_DIDWEB_INSECURE_HOSTS`` — comma-separated exact hosts or
    ``.suffix`` matches, e.g. ``.aitp.test``) lets the Level 1 federated
    stack resolve did:web over http across distinct hostnames without TLS.
    Production leaves the env var unset, so real did:web stays https-only.
    """
    bare = host.split(":", 1)[0]
    if bare == "localhost" or bare.startswith("127."):
        return True
    allow = os.environ.get("AITP_DIDWEB_INSECURE_HOSTS", "")
    for entry in (e.strip() for e in allow.split(",") if e.strip()):
        if entry.startswith(".") and (bare == entry[1:] or bare.endswith(entry)):
            return True
        # Entries may carry a port (matching a did:web host verbatim); the
        # allowlist is about hostnames, so compare port-insensitively.
        if bare == entry.split(":", 1)[0]:
            return True
    return False


async def resolve_did_web(did: str) -> str:
    """did:web:host[%3Aport] → manifest URL via /.well-known/did.json."""
    if not did.startswith("did:web:"):
        raise ValueError(f"not a did:web DID: {did}")
    host = unquote(did[len("did:web:"):])
    scheme = "http" if _http_allowed(host) else "https"
    url = f"{scheme}://{host}/.well-known/did.json"
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        doc = r.json()
    svc = next(
        (s for s in doc.get("service", []) if s.get("type") == "AitpManifest"),
        None,
    )
    if not svc:
        raise ValueError(f"no AitpManifest service in DID document for {did}")
    endpoint = svc["serviceEndpoint"].rstrip("/")
    return f"{endpoint}/.well-known/aitp-manifest"


def encode_did_web(host: str) -> str:
    return f"did:web:{quote(host, safe='.')}"
