"""Timing regression test for the ``cp_delegation_tree`` step.

The step must flush this run's observed events to the CP and await the ingest
*before* querying the delegation projection. Agent canonical delegation events
reach the playground store via /internal/telemetry but not the CP until ingest,
so querying first races projection and returns an empty tree (see
plans/cp-delegation-tree-timing-fix.md).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aitp_playground.hosting.supervisor import RunningAgent
from aitp_playground.registry.models import WorkflowStep
from aitp_playground.runner.context import RunContext
from aitp_playground.runner.engine import ScenarioRunner


def _engine(cp: object, store: object) -> ScenarioRunner:
    """Build an engine with only the collaborators the step touches mocked.

    ``cp_delegation_tree`` reads ``self.cp`` and ``self.store`` only; the rest
    are irrelevant to this step and never invoked here.
    """
    return ScenarioRunner(
        registry=MagicMock(),
        supervisor=MagicMock(),
        bootstrap_builder=MagicMock(),
        adapters=MagicMock(),
        trust=MagicMock(),
        cp=cp,
        port_alloc=MagicMock(),
        config=MagicMock(),
        store=store,
    )


@pytest.mark.asyncio
async def test_cp_delegation_tree_ingests_before_fetching() -> None:
    calls: list[str] = []

    cp = MagicMock()
    cp.enabled = True
    events_so_far = [{"type": "delegation.issued", "jti": "abc"}]
    delegation_row = {"jti": "abc", "delegator": "aid:researcher"}

    async def _ingest(events: object) -> None:
        # The exact store events must be flushed, not ctx.events.
        assert events == events_so_far
        calls.append("ingest")

    async def _fetch(*, delegator: str) -> list[dict]:
        assert delegator == "aid:researcher"
        calls.append("fetch")
        return [delegation_row]

    cp.ingest_events = AsyncMock(side_effect=_ingest)
    cp.fetch_delegations = AsyncMock(side_effect=_fetch)

    store = MagicMock()
    store.get.return_value = {"events": events_so_far}

    engine = _engine(cp, store)

    ctx = RunContext(run_id="run-1", scenario_ref="intra-org/cp-delegation-tree@1.0.0")
    running = {
        "researcher": RunningAgent(
            run_id="run-1",
            agent_id="researcher",
            port=9000,
            pid=123,
            aid="aid:researcher",
            manifest_url="https://researcher.local/manifest",
        )
    }
    step = WorkflowStep(id="tree", type="cp_delegation_tree", agent="researcher")
    outputs: dict = {}

    await engine._dispatch_step(step, MagicMock(), running, {}, {}, {}, outputs, ctx)

    # Ingest must precede the query, otherwise the projection is read empty.
    assert calls == ["ingest", "fetch"]
    store.get.assert_called_once_with("run-1")
    assert outputs["tree"] == {
        "delegator": "aid:researcher",
        "delegations": [delegation_row],
        "count": 1,
    }
    # The step emits a cp.delegation.tree event with the populated result.
    tree_events = [e for e in ctx.events if e.type == "cp.delegation.tree"]
    assert tree_events and tree_events[0].result["count"] == 1


@pytest.mark.asyncio
async def test_cp_delegation_tree_skips_without_cp() -> None:
    cp = MagicMock()
    cp.enabled = False
    cp.ingest_events = AsyncMock()
    cp.fetch_delegations = AsyncMock()

    engine = _engine(cp, MagicMock())

    ctx = RunContext(run_id="run-2", scenario_ref="intra-org/cp-delegation-tree@1.0.0")
    running = {
        "researcher": RunningAgent(
            run_id="run-2",
            agent_id="researcher",
            port=9000,
            pid=123,
            aid="aid:researcher",
            manifest_url="https://researcher.local/manifest",
        )
    }
    step = WorkflowStep(id="tree", type="cp_delegation_tree", agent="researcher")
    outputs: dict = {}

    await engine._dispatch_step(step, MagicMock(), running, {}, {}, {}, outputs, ctx)

    # With no CP configured neither the flush nor the query runs.
    cp.ingest_events.assert_not_called()
    cp.fetch_delegations.assert_not_called()
    assert outputs["tree"] == {"delegations": [], "skipped": "no cp"}
