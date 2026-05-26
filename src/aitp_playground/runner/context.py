"""Per-run mutable state shared across the engine."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    type: str
    ts: float = Field(default_factory=time.time)

    # Arbitrary, type-tag-dependent fields:
    run_id: Optional[str] = None
    scenario_ref: Optional[str] = None
    template: Optional[str] = None
    agent_id: Optional[str] = None
    agent: Optional[str] = None
    aid: Optional[str] = None
    port: Optional[int] = None
    step_id: Optional[str] = None
    capability: Optional[str] = None
    initiator: Optional[str] = None
    target: Optional[str] = None
    grants: list[str] | None = None
    peers: dict[str, Any] | None = None
    result: Any | None = None
    error: Optional[str] = None
    notes: Optional[str] = None
    # Used by trust.established (for revocation lookup) and tct.revoked.
    jti: Optional[str] = None


@dataclass
class RunContext:
    run_id: str
    scenario_ref: str
    run_label: Optional[str] = None
    events: list[RunEvent] = field(default_factory=list)
    # Optional RunStore — when set, every emit() also fans out to SSE subscribers.
    store: Any = field(default=None, repr=False)

    def emit(self, event: RunEvent) -> None:
        if event.run_id is None:
            event.run_id = self.run_id
        self.events.append(event)
        if self.store is not None:
            self.store.append_event(self.run_id, event.model_dump())
        # Side-effect: record into the metrics registry. Importing here
        # (rather than at module top) keeps the runner package free of a
        # hard dependency on the observability package — if a downstream
        # ever wants to vendor the runner without metrics, the import
        # can be guarded.
        try:
            from ..observability.metrics import record_event
            record_event(event.model_dump())
        except Exception:  # noqa: BLE001
            # Metrics must never break a run.
            pass
