"""RunResult: terminal value returned by ScenarioRunner.run()."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .context import RunEvent


@dataclass
class RunResult:
    run_id: str
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    events: list[RunEvent] = field(default_factory=list)
    error: Optional[str] = None

    @classmethod
    def success(
        cls,
        run_id: str,
        outputs: dict[str, Any],
        events: list[RunEvent],
    ) -> "RunResult":
        return cls(run_id=run_id, status="success", outputs=outputs, events=events)

    @classmethod
    def failure(
        cls,
        run_id: str,
        error: str,
        events: list[RunEvent] | None = None,
    ) -> "RunResult":
        return cls(run_id=run_id, status="failed", outputs={}, events=events or [], error=error)
