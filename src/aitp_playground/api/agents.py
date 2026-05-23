"""Running-agents introspection."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from ..hosting.supervisor import AgentSupervisor
from ._deps import get_supervisor

router = APIRouter(tags=["agents"])


@router.get("/agents")
def list_running_agents(supervisor: AgentSupervisor = Depends(get_supervisor)) -> dict:
    return {"agents": [asdict(a) for a in supervisor.list_running()]}
