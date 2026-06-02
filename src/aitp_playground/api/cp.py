"""Control Plane observability proxies (not run-scoped).

These surface the CP's entity-keyed projections — TCTs, delegation chains,
session replay, dashboards, and trust configuration — which the CP filters by
entity fields (issuer, root_jti, …) rather than by run. Every route degrades
to ``cp_enabled=false`` with an empty payload when no CP is configured, so a
CLI or dashboard can call them unconditionally.

Run-scoped CP proxies (audit / sessions / deliveries) live on the /runs
router instead.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..cp_client.client import CpClient
from ._deps import get_cp_client

router = APIRouter(prefix="/cp", tags=["control-plane"])


class CpListResponse(BaseModel):
    cp_enabled: bool
    items: list[dict[str, Any]] = []
    count: int = 0


class CpObjectResponse(BaseModel):
    cp_enabled: bool
    data: dict[str, Any] = {}


def _list(enabled: bool, items: list[dict[str, Any]]) -> CpListResponse:
    return CpListResponse(cp_enabled=enabled, items=items, count=len(items))


@router.get("/tcts", response_model=CpListResponse)
async def list_tcts(
    issuer: Optional[str] = None,
    subject: Optional[str] = None,
    audience: Optional[str] = None,
    capability: Optional[str] = None,
    session_id: Optional[str] = None,
    active: Optional[bool] = None,
    limit: int = 200,
    cp: CpClient = Depends(get_cp_client),
) -> CpListResponse:
    """Proxy ``GET /api/tcts`` — TCTs the CP has observed."""
    if not cp.enabled:
        return _list(False, [])
    items = await cp.fetch_tcts(
        issuer=issuer, subject=subject, audience=audience,
        capability=capability, session_id=session_id, active=active, limit=limit,
    )
    return _list(True, items)


@router.get("/delegations", response_model=CpListResponse)
async def list_delegations(
    root_jti: Optional[str] = None,
    parent_jti: Optional[str] = None,
    delegator: Optional[str] = None,
    delegatee: Optional[str] = None,
    active: Optional[bool] = None,
    limit: int = 200,
    cp: CpClient = Depends(get_cp_client),
) -> CpListResponse:
    """Proxy ``GET /api/delegations``. ``root_jti`` walks the whole chain."""
    if not cp.enabled:
        return _list(False, [])
    items = await cp.fetch_delegations(
        root_jti=root_jti, parent_jti=parent_jti, delegator=delegator,
        delegatee=delegatee, active=active, limit=limit,
    )
    return _list(True, items)


@router.get("/sessions/{session_id}/replay", response_model=CpListResponse)
async def replay_session(
    session_id: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 500,
    cp: CpClient = Depends(get_cp_client),
) -> CpListResponse:
    """Proxy ``GET /api/sessions/{id}/replay`` — ordered session event stream."""
    if not cp.enabled:
        return _list(False, [])
    items = await cp.replay_session(session_id, since=since, until=until, limit=limit)
    return _list(True, items)


@router.get("/dashboard", response_model=CpObjectResponse)
async def dashboard_overview(
    window: str = "24h",
    cp: CpClient = Depends(get_cp_client),
) -> CpObjectResponse:
    """Proxy ``GET /api/dashboard/overview`` — aggregate CP metrics."""
    if not cp.enabled:
        return CpObjectResponse(cp_enabled=False)
    return CpObjectResponse(cp_enabled=True, data=await cp.fetch_dashboard_overview(window))


@router.get("/agents", response_model=CpListResponse)
async def dashboard_agents(cp: CpClient = Depends(get_cp_client)) -> CpListResponse:
    """Proxy ``GET /api/dashboard/agents`` — per-agent CP metrics."""
    if not cp.enabled:
        return _list(False, [])
    return _list(True, await cp.fetch_dashboard_agents())


@router.get("/trust-anchors", response_model=CpListResponse)
async def list_trust_anchors(
    namespace: Optional[str] = None,
    cp: CpClient = Depends(get_cp_client),
) -> CpListResponse:
    """Proxy ``GET /api/trust-anchors`` — configured OIDC issuer bindings."""
    if not cp.enabled:
        return _list(False, [])
    return _list(True, await cp.list_trust_anchors(namespace=namespace))


@router.get("/pinned-keys", response_model=CpListResponse)
async def list_pinned_keys(
    namespace: Optional[str] = None,
    cp: CpClient = Depends(get_cp_client),
) -> CpListResponse:
    """Proxy ``GET /api/pinned-keys`` — registered pinned keys."""
    if not cp.enabled:
        return _list(False, [])
    return _list(True, await cp.list_pinned_keys(namespace=namespace))
