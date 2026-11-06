"""Peer resolver utilities — pure HTTP/DID lookups, no AITP crypto."""
from __future__ import annotations

from urllib.parse import quote, unquote

import httpx

from ..hosting.loopback import AgentLoopback


def _is_loopback_host(host: str) -> bool:
    bare = host.split(":", 1)[0]
    return bare == "localhost" or bare.startswith("127.")


async def resolve_did_web(did: str, loopback: AgentLoopback) -> str:
    """did:web:host[%3Aport] → manifest URL via /.well-known/did.json.

    https-only, except a loopback host under the local/test opt-in."""
    if not did.startswith("did:web:"):
        raise ValueError(f"not a did:web DID: {did}")
    host = unquote(did[len("did:web:"):])
    scheme = loopback.scheme if _is_loopback_host(host) else "https"
    url = f"{scheme}://{host}/.well-known/did.json"
    async with httpx.AsyncClient(timeout=5.0, mounts=loopback.mounts()) as client:
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
