"""Scenario run endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..errors import RunNotFoundError
from ..hosting.supervisor import AgentSupervisor
from ..runner.engine import ScenarioRunner
from ..runner.store import RunStore
from ._deps import get_run_store, get_runner, get_supervisor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runs", tags=["runs"])


class RunRequest(BaseModel):
    scenario_ref: str
    inputs: dict[str, Any] = {}
    run_label: Optional[str] = None


class RunCreated(BaseModel):
    run_id: str
    status: str
    scenario_ref: str


class RunSummary(BaseModel):
    run_id: str
    status: Optional[str] = None
    scenario_ref: Optional[str] = None
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
        "outputs": {},
        "events": [],
        "error": None,
    })
    background_tasks.add_task(_run_in_background, runner, run_id, body)
    return RunCreated(run_id=run_id, status="pending", scenario_ref=body.scenario_ref)


async def _run_in_background(runner: ScenarioRunner, run_id: str, body: RunRequest) -> None:
    try:
        await runner.run(
            run_id=run_id,
            scenario_ref=body.scenario_ref,
            inputs=body.inputs,
            run_label=body.run_label,
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
        outputs=record.get("outputs") or {},
        events=list(record.get("events") or []),
        error=record.get("error"),
        created_at=record.get("created_at"),
    )


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
    """Cancel an in-flight run. Kills the spawned agent subprocesses; the
    background task will subsequently fail on the next inter-agent HTTP call
    and finalize the run as failed (a follow-up upsert here promotes that to
    `cancelled`). No-op for terminal runs."""
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
    supervisor.kill_run(run_id)
    store.upsert(run_id, {"status": "cancelled"})
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
