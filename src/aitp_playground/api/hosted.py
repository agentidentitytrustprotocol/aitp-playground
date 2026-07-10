"""Hosted-agent federation API.

Stands up long-lived agents at this service's public origin and drives
cross-domain trust against peers hosted on *other* services. Used by the
Level 1 / Level 2 federated e2e stack, where org-A hosts the researcher and
org-B hosts the analyzer and the researcher resolves + handshakes the analyzer
across a real origin boundary.

did:web resolution here is deliberately fail-closed: if a peer DID cannot be
resolved to a non-loopback origin the request errors instead of silently
falling back to localhost, so a green test can never be a disguised
same-process handshake.
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..hosting.hosted import HostedAgentManager
from ..trust.resolver import resolve_did_web
from ._deps import get_hosted_manager

router = APIRouter(prefix="/hosted-agents", tags=["hosted-agents"])


class HostRequest(BaseModel):
    ref: str
    public_host: Optional[str] = None
    public_scheme: Optional[str] = None
    signing_suite: Optional[str] = None
    inputs: Optional[dict[str, Any]] = None
    port: Optional[int] = None


class HandshakeRequest(BaseModel):
    peer_did: str
    requested_grants: Optional[list[str]] = None


class InvokeRequest(BaseModel):
    peer_port: int
    capability: str
    peer_base_url: Optional[str] = None
    payload: Optional[Any] = None


def _origin_of(manifest_url: str) -> str:
    """Strip the well-known suffix to recover the peer's base origin."""
    return manifest_url.rsplit("/.well-known/", 1)[0].rstrip("/")


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host == "localhost" or host.startswith("127.")


@router.post("")
async def host_agent(
    body: HostRequest, mgr: HostedAgentManager = Depends(get_hosted_manager)
) -> dict[str, Any]:
    try:
        hosted = await mgr.host(
            ref=body.ref,
            public_host=body.public_host,
            public_scheme=body.public_scheme,
            signing_suite=body.signing_suite,
            inputs=body.inputs,
            port=body.port,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"failed to host agent: {exc}") from exc
    from dataclasses import asdict

    return asdict(hosted)


@router.get("")
def list_agents(mgr: HostedAgentManager = Depends(get_hosted_manager)) -> dict[str, Any]:
    return {"hosted": mgr.list()}


@router.get("/{hosted_id}")
def get_agent(
    hosted_id: str, mgr: HostedAgentManager = Depends(get_hosted_manager)
) -> dict[str, Any]:
    hosted = mgr.get(hosted_id)
    if hosted is None:
        raise HTTPException(status_code=404, detail=f"no hosted agent {hosted_id}")
    from dataclasses import asdict

    return asdict(hosted)


@router.delete("/{hosted_id}")
def stop_agent(
    hosted_id: str, mgr: HostedAgentManager = Depends(get_hosted_manager)
) -> dict[str, Any]:
    if not mgr.stop(hosted_id):
        raise HTTPException(status_code=404, detail=f"no hosted agent {hosted_id}")
    return {"stopped": hosted_id}


@router.post("/{hosted_id}/resolve-and-handshake")
async def resolve_and_handshake(
    hosted_id: str,
    body: HandshakeRequest,
    mgr: HostedAgentManager = Depends(get_hosted_manager),
) -> dict[str, Any]:
    """Resolve ``peer_did`` via did:web (fail-closed) and drive this hosted
    agent's SDK handshake against the resolved peer origin."""
    hosted = mgr.get(hosted_id)
    if hosted is None:
        raise HTTPException(status_code=404, detail=f"no hosted agent {hosted_id}")

    if not body.peer_did.startswith("did:web:"):
        raise HTTPException(
            status_code=400, detail=f"only did:web peers supported, got {body.peer_did}"
        )
    try:
        peer_manifest_url = await resolve_did_web(body.peer_did)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"did:web resolution failed for {body.peer_did}: {exc}",
        ) from exc

    peer_origin = _origin_of(peer_manifest_url)
    # Fail-closed: a cross-domain handshake that resolved to loopback is a
    # same-process handshake wearing a did:web costume. Refuse it — unless a
    # test explicitly opts in via AITP_FEDERATION_ALLOW_LOOPBACK, which lets
    # the in-process integration suite exercise the full path over two real
    # sockets on 127.0.0.1 without Docker. The Docker e2e leaves it unset.
    allow_loopback = os.environ.get("AITP_FEDERATION_ALLOW_LOOPBACK", "").lower() in (
        "1", "true", "yes",
    )
    if _is_loopback(peer_manifest_url) and not allow_loopback:
        raise HTTPException(
            status_code=409,
            detail=(
                f"refusing cross-domain handshake: {body.peer_did} resolved to a "
                f"loopback origin ({peer_origin}); expected a real remote origin"
            ),
        )
    expected_host = unquote(body.peer_did[len("did:web:"):])
    resolved_host = peer_origin.split("://", 1)[-1]
    if resolved_host != expected_host:
        raise HTTPException(
            status_code=409,
            detail=(
                f"did:web origin mismatch: {body.peer_did} resolved to "
                f"{resolved_host!r}, expected {expected_host!r}"
            ),
        )

    admin = f"http://localhost:{hosted.port}/admin/initiate-handshake"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                admin,
                json={
                    "peer_manifest_url": peer_manifest_url,
                    "requested_grants": body.requested_grants,
                },
            )
            r.raise_for_status()
            result = r.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"handshake failed ({exc.response.status_code}): {exc.response.text}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"handshake failed: {exc}") from exc

    return {
        "trust": "established",
        "peer_did": body.peer_did,
        "resolved_manifest_url": peer_manifest_url,
        "peer_origin": peer_origin,
        "peer_base_url": peer_origin,
        **result,  # grants, peer_aid, peer_port, session_id, jti
    }


@router.post("/{hosted_id}/invoke")
async def invoke(
    hosted_id: str,
    body: InvokeRequest,
    mgr: HostedAgentManager = Depends(get_hosted_manager),
) -> dict[str, Any]:
    """Invoke ``capability`` on a peer (at ``peer_base_url``) using the TCT this
    hosted agent obtained during the handshake."""
    hosted = mgr.get(hosted_id)
    if hosted is None:
        raise HTTPException(status_code=404, detail=f"no hosted agent {hosted_id}")

    admin = f"http://localhost:{hosted.port}/admin/invoke"
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                admin,
                json={
                    "peer_port": body.peer_port,
                    "peer_base_url": body.peer_base_url,
                    "capability": body.capability,
                    "payload": body.payload,
                },
            )
            r.raise_for_status()
            return {"result": r.json()}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"invoke failed ({exc.response.status_code}): {exc.response.text}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"invoke failed: {exc}") from exc
