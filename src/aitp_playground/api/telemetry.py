"""Internal telemetry sink for agent subprocesses."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from ..observability.metrics import LLM_QUOTA_DENIALS_TOTAL, metrics
from ._deps import require_telemetry_token
from .quota import REASON_CHECK_FAILED, is_scenario_run, record_llm_fallback

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
    if body.get("type") == "llm.quota_exceeded":
        # An agent reports this only when it could not get an answer from
        # /internal/llm-quota (a decided denial is recorded there). Fold it
        # into the run's single quota event; a duplicate is dropped. The
        # reason is fixed, never taken from the body: it is a metric label,
        # and a decided denial is only ever recorded by the quota route.
        reason = REASON_CHECK_FAILED
        metrics.inc(LLM_QUOTA_DENIALS_TOTAL, {"reason": reason})
        if is_scenario_run(run_id):
            calls = body.get("calls")
            record_llm_fallback(
                store, str(run_id), reason=reason,
                calls=calls if isinstance(calls, int) and 0 < calls <= 100 else 1,
                capability=body.get("capability"), agent_id=body.get("agent_id"),
            )
        return {"ok": True}
    if run_id:
        store.append_event(run_id, body)
    return {"ok": True}
