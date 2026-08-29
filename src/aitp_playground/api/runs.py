"""Scenario run endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from ..cp_client.client import CpClient
from ..errors import RunNotFoundError
from ..hosting.supervisor import AgentSupervisor
from ..observability.metrics import record_event
from ..observability.narrator import narrate_events
from ..runner.context import RunEvent
from ..runner.engine import ScenarioRunner
from ..runner.store import RunStore
from ._deps import get_cp_client, get_run_store, get_runner, get_supervisor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runs", tags=["runs"])


class RunRequest(BaseModel):
    scenario_ref: str
    inputs: dict[str, Any] = {}
    run_label: Optional[str] = None
    # Optional template variant declared under
    # ``scenarios/<pack>/<scenario>/<version>/templates/<template>.yaml``.
    # When set, the runner merges it on top of the base scenario before
    # executing — same scenario_ref, different workflow / trust posture.
    template: Optional[str] = None


class RunCreated(BaseModel):
    run_id: str
    status: str
    scenario_ref: str
    run_label: Optional[str] = None


class RunSummary(BaseModel):
    run_id: str
    status: Optional[str] = None
    scenario_ref: Optional[str] = None
    run_label: Optional[str] = None
    created_at: Optional[float] = None
    event_count: int = 0


class RunList(BaseModel):
    runs: list[RunSummary]


class RunStatus(BaseModel):
    run_id: str
    status: Optional[str] = None
    event_count: int = 0
    created_at: Optional[float] = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    scenario_ref: str
    run_label: Optional[str] = None
    outputs: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    error: Optional[str] = None
    created_at: Optional[float] = None


@router.post("", response_model=RunCreated, status_code=202)
async def create_run(
    background_tasks: BackgroundTasks,
    body: RunRequest,
    runner: ScenarioRunner = Depends(get_runner),
    store: RunStore = Depends(get_run_store),
) -> RunCreated:
    """Start a scenario run asynchronously. Returns immediately with a run_id;
    callers poll `GET /runs/{id}` or stream `GET /runs/{id}/events` to observe."""
    run_id = str(uuid.uuid4())
    store.upsert(run_id, {
        "run_id": run_id,
        "status": "pending",
        "scenario_ref": body.scenario_ref,
        "run_label": body.run_label,
        "outputs": {},
        "events": [],
        "error": None,
    })
    background_tasks.add_task(_run_in_background, runner, run_id, body)
    return RunCreated(
        run_id=run_id,
        status="pending",
        scenario_ref=body.scenario_ref,
        run_label=body.run_label,
    )


async def _run_in_background(runner: ScenarioRunner, run_id: str, body: RunRequest) -> None:
    try:
        await runner.run(
            run_id=run_id,
            scenario_ref=body.scenario_ref,
            inputs=body.inputs,
            run_label=body.run_label,
            template=body.template,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Background run %s crashed", run_id)


@router.get("", response_model=RunList)
def list_runs(store: RunStore = Depends(get_run_store)) -> RunList:
    items: list[RunSummary] = []
    for rid in store.list_ids():
        r = store.get(rid) or {}
        items.append(RunSummary(
            run_id=rid,
            status=r.get("status"),
            scenario_ref=r.get("scenario_ref"),
            run_label=r.get("run_label"),
            created_at=r.get("created_at"),
            event_count=len(r.get("events", [])),
        ))
    return RunList(runs=items)


@router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: str, store: RunStore = Depends(get_run_store)) -> RunResponse:
    record = store.get(run_id)
    if record is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    return RunResponse(
        run_id=record["run_id"],
        status=record.get("status") or "unknown",
        scenario_ref=record.get("scenario_ref") or "",
        run_label=record.get("run_label"),
        outputs=record.get("outputs") or {},
        events=list(record.get("events") or []),
        error=record.get("error"),
        created_at=record.get("created_at"),
    )


class CpAuditResponse(BaseModel):
    run_id: str
    cp_enabled: bool
    events: list[dict[str, Any]] = []
    event_types: list[str] = []
    count: int = 0


class CpSessionsResponse(BaseModel):
    run_id: str
    cp_enabled: bool
    sessions: list[dict[str, Any]] = []
    count: int = 0


@router.get("/{run_id}/cp-audit", response_model=CpAuditResponse)
async def get_run_cp_audit(
    run_id: str,
    type: Optional[str] = None,
    limit: int = 200,
    store: RunStore = Depends(get_run_store),
    cp: CpClient = Depends(get_cp_client),
) -> CpAuditResponse:
    """Proxy the slice of the Control Plane audit log that belongs to this run.

    The CP-side query is ``GET /api/events/history?run_id={id}`` with the
    optional ``type=`` filter passed through. When CP isn't configured the
    response is ``cp_enabled=false`` with an empty list — callers can
    branch on this without having to know CP wiring.
    """
    if store.get(run_id) is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    if not cp.enabled:
        return CpAuditResponse(run_id=run_id, cp_enabled=False)
    events = await cp.fetch_events_history(run_id=run_id, type_=type, limit=limit)
    return CpAuditResponse(
        run_id=run_id,
        cp_enabled=True,
        events=events,
        event_types=sorted({e.get("type") for e in events if e.get("type")}),
        count=len(events),
    )


class CpDeliveriesResponse(BaseModel):
    run_id: str
    subscribed: bool
    webhook: Optional[dict[str, Any]] = None
    deliveries: list[dict[str, Any]] = []
    count: int = 0


@router.get("/{run_id}/cp-deliveries", response_model=CpDeliveriesResponse)
def get_run_cp_deliveries(
    run_id: str,
    event_type: Optional[str] = None,
    store: RunStore = Depends(get_run_store),
) -> CpDeliveriesResponse:
    """List CP webhook deliveries this run has received.

    Deliveries come from CP's audit fan-out into ``POST /webhooks/cp/{run_id}``;
    each one is also visible in the main event log as
    ``cp.webhook.delivered``. This endpoint surfaces just those rows so a
    CLI / dashboard doesn't have to filter the full event list itself.
    Pass ``event_type=`` to filter to a single CP event class
    (``handshake.complete``, ``tct.revoked``, etc.).
    """
    record = store.get(run_id)
    if record is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    cp_webhook = record.get("cp_webhook")
    events = [
        e for e in (record.get("events") or [])
        if e.get("type") == "cp.webhook.delivered"
        and (event_type is None or e.get("event_type") == event_type)
    ]
    # Strip the run-secret from the webhook block before exposing.
    webhook_view = None
    if cp_webhook:
        webhook_view = {k: v for k, v in cp_webhook.items() if k != "secret"}
    return CpDeliveriesResponse(
        run_id=run_id,
        subscribed=bool(cp_webhook),
        webhook=webhook_view,
        deliveries=events,
        count=len(events),
    )


@router.get("/{run_id}/cp-sessions", response_model=CpSessionsResponse)
async def get_run_cp_sessions(
    run_id: str,
    status: Optional[str] = None,
    limit: int = 200,
    store: RunStore = Depends(get_run_store),
    cp: CpClient = Depends(get_cp_client),
) -> CpSessionsResponse:
    """Proxy the Control Plane's handshake-session records for this run.

    The CP-side query is ``GET /api/sessions?run_id={id}`` with the
    optional ``status=`` filter passed through.
    """
    if store.get(run_id) is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    if not cp.enabled:
        return CpSessionsResponse(run_id=run_id, cp_enabled=False)
    sessions = await cp.fetch_sessions(run_id=run_id, status=status, limit=limit)
    return CpSessionsResponse(
        run_id=run_id, cp_enabled=True, sessions=sessions, count=len(sessions),
    )


@router.get("/{run_id}/narrate", response_class=PlainTextResponse)
def get_run_narrate(
    run_id: str,
    store: RunStore = Depends(get_run_store),
) -> PlainTextResponse:
    """Return a human-readable narration of this run's event log.

    Each protocol step (handshake, delegate, redeem, revoke, rotate,
    enroll, fault) becomes a single short line. Unknown event types are
    dropped — the raw log is still available via ``GET /runs/{id}``.
    Useful for live tail (``curl -N`` over a long run) and for the
    ``aitp-playground trace`` CLI subcommand.
    """
    record = store.get(run_id)
    if record is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    events = list(record.get("events") or [])
    lines = narrate_events(events)
    # Add a trailing summary line so the output is self-contained.
    status = record.get("status") or "unknown"
    lines.append(f"[run] status={status}  events={len(events)}  narrated={len(lines)-0}")
    return PlainTextResponse("\n".join(lines) + "\n")


@router.get("/{run_id}/status", response_model=RunStatus)
def get_run_status(run_id: str, store: RunStore = Depends(get_run_store)) -> RunStatus:
    record = store.get(run_id)
    if record is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    return RunStatus(
        run_id=run_id,
        status=record.get("status"),
        event_count=len(record.get("events", [])),
        created_at=record.get("created_at"),
    )


_TERMINAL_STATUSES = {"success", "failed", "cancelled"}


@router.post("/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: str,
    store: RunStore = Depends(get_run_store),
    supervisor: AgentSupervisor = Depends(get_supervisor),
) -> dict[str, Any]:
    """Cancel an in-flight run. Marks the record `cancelled` and emits
    `run.cancelled` BEFORE killing the spawned agent subprocesses — order
    matters here, not just presence: killing a subprocess can turn the
    background task's next inter-agent HTTP call into an exception almost
    immediately (a different thread, no synchronization with this
    function), so upserting `cancelled` only after the kill is a real race —
    `_finalize_failure`'s guard reads a store that may not say `cancelled`
    yet. Mark first, kill second, and the guard always has something to see.
    No-op for terminal runs.

    Emits `run.cancelled` directly (there is no `RunContext` at this layer —
    the background task owns that) via the same two side effects
    `RunContext.emit` performs: append to the store's event log, and record
    into the metrics registry. `_finalize_failure`'s guard is what keeps this
    from being followed by a `run.failed` for the same run, which would
    double-count one run into two `runs_total` labels. See `DECISIONS.md`
    D-16 for the race this ordering closes, found live rather than reasoned."""
    record = store.get(run_id)
    if record is None:
        raise RunNotFoundError(f"Run {run_id} not found")
    if record.get("status") in _TERMINAL_STATUSES:
        return {
            "run_id": run_id,
            "status": record.get("status"),
            "cancelled": False,
            "reason": "already terminal",
        }
    store.upsert(run_id, {"status": "cancelled"})
    event = RunEvent(type="run.cancelled", run_id=run_id)
    store.append_event(run_id, event.model_dump())
    try:
        record_event(event.model_dump())
    except Exception:  # noqa: BLE001 — metrics must never break cancellation
        logger.exception("record_event failed for run.cancelled on %s", run_id)
    supervisor.kill_run(run_id)
    return {"run_id": run_id, "status": "cancelled", "cancelled": True}


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    store: RunStore = Depends(get_run_store),
) -> StreamingResponse:
    """Server-Sent Events stream of run events. Replays backlog first, then
    streams new events live, sending a heartbeat every second while idle.
    Closes with `data: {"type":"stream.end"}` once the run is terminal and the
    queue has drained."""
    if store.get(run_id) is None:
        raise RunNotFoundError(f"Run {run_id} not found")

    async def gen() -> AsyncIterator[bytes]:
        q, backlog = store.subscribe(run_id)
        try:
            for evt in backlog:
                yield f"data: {json.dumps(evt)}\n\n".encode()
            while True:
                record = store.get(run_id) or {}
                terminal = record.get("status") in _TERMINAL_STATUSES
                if terminal and q.empty():
                    yield b"data: {\"type\":\"stream.end\"}\n\n"
                    return
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield f"data: {json.dumps(evt)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        finally:
            store.unsubscribe(run_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
