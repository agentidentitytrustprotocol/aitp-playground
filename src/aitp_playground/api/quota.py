"""The LLM quota gate agents ask before any real provider call.

``POST /internal/llm-quota`` charges the declared call units for one
capability against the quota store and answers ``{allowed, reason, quota,
limit, resets_at}``. A denied agent runs that whole capability on its
deterministic stub, so a step's output is never half real, half mock.

The playground, not the agent, decides who is charged: the subject IP comes
from the run record written at launch (``subject_ip``; null for keyless
traffic and hosted agents, which charge the global counters only). The first
denial in a run marks the run summary (``llm_quota_exceeded``,
``llm_quota``) and emits one ``llm.quota_exceeded`` event; every denial adds
its units to ``llm_calls_on_mock``.

Reachable only from a direct loopback peer, always (agents dial
``PLAYGROUND_INTERNAL_URL``), plus the per-process telemetry token.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..errors import RunNotFoundError
from ..observability.metrics import (
    LLM_QUOTA_CHARGED_TOTAL,
    LLM_QUOTA_DENIALS_TOTAL,
    LLM_QUOTA_STORE_UP,
    metrics,
)
from ..quota import REASON_UNAVAILABLE
from ..runner.context import RunEvent
from ..security import is_direct_loopback
from ._deps import require_telemetry_token

logger = logging.getLogger("aitp_playground.quota")

router = APIRouter(prefix="/internal", tags=["telemetry"])

#: Hosted agents (no scenario run) carry ``hosted-<id>`` as their run id.
HOSTED_PREFIX = "hosted-"
#: Reason an agent reports when it could not get an answer from this endpoint.
REASON_CHECK_FAILED = "quota_check_failed"


def require_direct_loopback(request: Request) -> None:
    """Refuse anything but a direct loopback peer, whatever the key flag says."""
    if not is_direct_loopback(request.scope):
        raise HTTPException(status_code=403, detail="loopback only")


class LlmQuotaRequest(BaseModel):
    run_id: Optional[str] = Field(default=None, max_length=200)
    capability: str = Field(min_length=1, max_length=200)
    calls: int = Field(ge=1, le=100)
    agent_id: Optional[str] = Field(default=None, max_length=200)


def is_scenario_run(run_id: Optional[str]) -> bool:
    return bool(run_id) and not str(run_id).startswith(HOSTED_PREFIX)


def record_llm_fallback(
    store: Any,
    run_id: str,
    *,
    reason: str,
    calls: int,
    capability: Optional[str] = None,
    agent_id: Optional[str] = None,
    quota: Optional[str] = None,
    limit: Optional[int] = None,
    resets_at: Optional[str] = None,
) -> bool:
    """Mark a run as having fallen back to the mock LLM.

    Adds ``calls`` to ``llm_calls_on_mock``; on the run's first fallback also
    sets ``llm_quota_exceeded`` and ``llm_quota`` and emits the single
    ``llm.quota_exceeded`` event (the check-and-set is one step under the
    store lock, so concurrent denials emit one event). Returns whether the
    event was emitted; False for an unknown run, which is never created.
    """
    first = False

    def mark(record: dict[str, Any]) -> None:
        nonlocal first
        record["llm_calls_on_mock"] = int(record.get("llm_calls_on_mock") or 0) + calls
        if not record.get("llm_quota_exceeded"):
            first = True
            record["llm_quota_exceeded"] = True
            record["llm_quota"] = {
                "reason": reason, "quota": quota, "limit": limit, "resets_at": resets_at,
            }

    if not store.update(run_id, mark) or not first:
        return False
    event = RunEvent(
        type="llm.quota_exceeded", run_id=run_id, capability=capability, agent_id=agent_id,
        code=reason, quota=quota, limit=limit, resets_at=resets_at, calls=calls,
        notes="real LLM calls refused by the quota; this run continues on the mock LLM",
    )
    store.append_event(run_id, event.model_dump())
    return True


@router.post(
    "/llm-quota",
    dependencies=[Depends(require_direct_loopback), Depends(require_telemetry_token)],
)
async def check_llm_quota(body: LlmQuotaRequest, request: Request) -> dict[str, Any]:
    run_store = request.app.state.run_store
    subject_ip: Optional[str] = None
    if is_scenario_run(body.run_id):
        record = run_store.get(body.run_id)
        if record is None:
            raise RunNotFoundError(f"Run {body.run_id} not found")
        subject_ip = record.get("subject_ip")

    decision = await request.app.state.quota_store.charge(subject_ip, body.calls)

    up = 0.0 if decision.reason == REASON_UNAVAILABLE else 1.0
    was_up = metrics.set(LLM_QUOTA_STORE_UP, up)
    if was_up != up:
        # Log the transition, not every refused call while the store is down.
        if up:
            logger.info("LLM quota store reachable")
        else:
            logger.warning("LLM quota store unavailable: refusing real LLM calls (fail closed)")
    if decision.allowed:
        metrics.inc(LLM_QUOTA_CHARGED_TOTAL, amount=float(body.calls))
        return decision.as_dict()

    metrics.inc(LLM_QUOTA_DENIALS_TOTAL, {"reason": str(decision.reason)})
    if is_scenario_run(body.run_id):
        record_llm_fallback(
            run_store, str(body.run_id), reason=str(decision.reason), calls=body.calls,
            capability=body.capability, agent_id=body.agent_id, quota=decision.quota,
            limit=decision.limit, resets_at=decision.resets_at,
        )
    return decision.as_dict()
