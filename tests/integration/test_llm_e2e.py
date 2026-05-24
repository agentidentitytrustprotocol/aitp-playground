"""End-to-end test: real CrewAI / LangChain / LangGraph agents, real OpenAI
calls, real AITP identity + trust.

Gated on ``AITP_LLM_E2E=1`` so it stays off the default suite. Intended to be
run inside the ``tests`` container of ``docker-compose.test.yml``, which sets:

    AITP_LLM_E2E=1
    PLAYGROUND_URL=http://playground:8000
    OPENAI_API_KEY=...           (from .env)
    LLM_PROVIDER=openai

Each scenario exercise:
  1. Launches real agent subprocesses (CrewAI / LangChain / LangGraph).
  2. Performs the AITP handshake + TCT issuance between every agent pair.
  3. Calls OpenAI through the framework's native LLM client.
  4. Asserts trust events fired AND the output text is from the real LLM
     (not the deterministic stub baked into each agent for offline runs).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AITP_LLM_E2E"),
    reason="Live LLM e2e — set AITP_LLM_E2E=1 (and OPENAI_API_KEY) to enable",
)

PLAYGROUND_URL = os.environ.get("PLAYGROUND_URL", "http://localhost:8000")

_TERMINAL = {"success", "failed", "cancelled"}
_RUN_DEADLINE_SECS = 300  # multi-agent CrewAI flows + OpenAI can take a while
_TRUST_EVENTS_REQUIRED = {"agent.ready", "trust.peers_resolved", "trust.established"}

# Marker strings emitted by each agent's deterministic stub. If any of these
# show up in the corresponding step's output, the real LLM path didn't run.
_STUB_MARKERS = {
    "researcher": "continues to attract significant research interest",
    "writer": "# Article (",
    "analyzer": "Misaligned grants between issuer and consumer",
}


@dataclass(frozen=True)
class ScenarioCase:
    ref: str
    inputs: dict
    # (step_id, which-agent-stub-to-watch-for) — fail if that stub marker
    # appears in this step's output.
    step_checks: tuple[tuple[str, str], ...]


SCENARIOS = [
    ScenarioCase(
        ref="intra-org/research-and-write@1.0.0",
        inputs={"topic": "AITP self-test"},
        step_checks=(("research", "researcher"), ("write", "writer")),
    ),
    ScenarioCase(
        ref="cross-cloud/distributed-review@1.0.0",
        inputs={"document": "Draft policy on AI agent data access"},
        step_checks=(
            ("draft", "researcher"),
            ("review", "writer"),
            ("approve", "analyzer"),
        ),
    ),
    ScenarioCase(
        ref="cross-org/federated-analysis@1.0.0",
        inputs={"topic": "agent identity protocols"},
        step_checks=(("research", "researcher"), ("analyze", "analyzer")),
    ),
]


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set — cannot exercise real LLM path")
    with httpx.Client(base_url=PLAYGROUND_URL, timeout=30.0) as c:
        # Pre-flight: the playground must be reachable and healthy.
        try:
            r = c.get("/healthz")
        except httpx.HTTPError as exc:
            pytest.fail(f"playground not reachable at {PLAYGROUND_URL}: {exc}")
        assert r.status_code == 200, f"/healthz returned {r.status_code}: {r.text}"
        yield c


def _start_run(client: httpx.Client, case: ScenarioCase) -> str:
    r = client.post("/runs", json={"scenario_ref": case.ref, "inputs": case.inputs})
    assert r.status_code == 202, f"POST /runs failed: {r.status_code} {r.text}"
    return r.json()["run_id"]


def _wait_for_terminal(client: httpx.Client, run_id: str) -> dict:
    deadline = time.time() + _RUN_DEADLINE_SECS
    body: dict = {}
    while time.time() < deadline:
        r = client.get(f"/runs/{run_id}")
        assert r.status_code == 200, f"GET /runs/{run_id}: {r.status_code} {r.text}"
        body = r.json()
        if body.get("status") in _TERMINAL:
            return body
        time.sleep(1.0)
    pytest.fail(
        f"run {run_id} did not finish within {_RUN_DEADLINE_SECS}s; "
        f"last status={body.get('status')!r}"
    )


def _output_text(step_output) -> str:
    """Stringify a step output for stub-marker scanning."""
    if step_output is None:
        return ""
    if isinstance(step_output, str):
        return step_output
    if isinstance(step_output, dict):
        # Concatenate all string-valued leaves so we catch nested fields like
        # {"summary": "...", "risks": "..."}.
        out: list[str] = []
        for v in step_output.values():
            out.append(_output_text(v))
        return "\n".join(out)
    if isinstance(step_output, list):
        return "\n".join(_output_text(v) for v in step_output)
    return str(step_output)


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda c: c.ref)
def test_scenario_runs_real_llm_under_aitp_trust(
    client: httpx.Client, case: ScenarioCase
) -> None:
    run_id = _start_run(client, case)
    body = _wait_for_terminal(client, run_id)

    assert body["status"] == "success", (
        f"{case.ref} did not succeed: status={body['status']!r} "
        f"error={body.get('error')!r}\nevents={body.get('events')}"
    )

    events = body.get("events", [])
    event_types = {e.get("type") for e in events}

    missing_trust = _TRUST_EVENTS_REQUIRED - event_types
    assert not missing_trust, (
        f"{case.ref}: missing required AITP trust events {sorted(missing_trust)}; "
        f"got {sorted(event_types)}"
    )

    # Real LLM invocations emit llm.started / llm.complete via the agent's
    # telemetry helper. (At least the researcher always emits these; writer
    # and analyzer don't, but the researcher path is on every scenario.)
    assert "llm.started" in event_types, (
        f"{case.ref}: expected llm.started event, got {sorted(event_types)}"
    )
    assert "llm.complete" in event_types, (
        f"{case.ref}: expected llm.complete event, got {sorted(event_types)}"
    )

    outputs = body.get("outputs") or {}

    for step_id, agent_kind in case.step_checks:
        assert step_id in outputs, (
            f"{case.ref}: step {step_id!r} missing from outputs {list(outputs.keys())}"
        )
        text = _output_text(outputs[step_id])
        assert text.strip(), f"{case.ref}: step {step_id!r} produced empty output"

        marker = _STUB_MARKERS[agent_kind]
        assert marker not in text, (
            f"{case.ref}: step {step_id!r} output contains the {agent_kind} "
            f"stub marker — the deterministic fallback ran instead of OpenAI. "
            f"Check OPENAI_API_KEY and LLM_PROVIDER inside the playground container."
        )
