"""Internal telemetry sink for agent subprocesses."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from ._deps import require_telemetry_token

logger = logging.getLogger("aitp_playground.telemetry")

router = APIRouter(prefix="/internal", tags=["telemetry"])


@router.post("/telemetry", dependencies=[Depends(require_telemetry_token)])
async def post_telemetry(request: Request) -> dict:
    """Agents POST AuditEvent-shaped JSON here. We log + append to the active run.

    Authenticated with the per-process telemetry token the playground writes
    into each agent's bootstrap file (``playground.telemetry_token``), not
    the API key — agent subprocesses never hold the API key."""
    body = await request.json()
    run_id = body.get("run_id") or body.get("playground", {}).get("run_id")
    logger.info("telemetry run=%s type=%s body=%s", run_id, body.get("type"), body)
    store = request.app.state.run_store
    if run_id:
        store.append_event(run_id, body)
    return {"ok": True}
