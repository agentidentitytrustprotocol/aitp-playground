"""Phase 1 wiring tests — no aitp-py required.

Covers the playground-side surfaces added for the TCT verification cache:
the new ``tct_cache_stats`` workflow step type and the tct-cache-perf
scenario loading through the registry.
"""
from __future__ import annotations

from aitp_playground.config import Settings
from aitp_playground.registry.models import WorkflowStep
from aitp_playground.registry.service import RegistryService


def test_tct_cache_stats_step_type_is_valid() -> None:
    step = WorkflowStep(id="cache-stats", type="tct_cache_stats", agent="writer")
    assert step.type == "tct_cache_stats"
    assert step.agent == "writer"


def test_tct_cache_perf_scenario_loads() -> None:
    svc = RegistryService(Settings())
    sv = svc.get_scenario("intra-org/tct-cache-perf@1.0.0")
    assert sv.metadata.name == "TCT Verification Cache"
    assert {a.id for a in sv.spec.agents} == {"researcher", "writer"}
    # The final step reads the writer's cache counters.
    last = sv.spec.workflow.steps[-1]
    assert last.type == "tct_cache_stats"
    assert last.agent == "writer"
    # Three repeat invocations precede it so the cache actually warms.
    write_steps = [
        s for s in sv.spec.workflow.steps if s.capability == "write.content"
    ]
    assert len(write_steps) == 3
