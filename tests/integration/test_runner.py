"""Runner integration test — actually spawns agent processes via aitp-py.

Gated on ``AITP_E2E=1`` so it stays off the default pytest path; subprocess
spawn + handshake + capability invoke takes ~30-45 seconds even with stub
agents. Run with:

    AITP_E2E=1 .venv/bin/pytest tests/integration/test_runner.py -v
"""
from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AITP_E2E"),
    reason="Live subprocess test — set AITP_E2E=1 to enable",
)

aitp = pytest.importorskip("aitp")

from fastapi.testclient import TestClient

from aitp_playground.main import create_app


_TERMINAL = {"success", "failed", "cancelled"}


def test_intra_org_research_and_write_end_to_end() -> None:
    client = TestClient(create_app())
    posted = client.post("/runs", json={
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "inputs": {"topic": "AITP self-test"},
    })
    assert posted.status_code == 202, posted.text
    rid = posted.json()["run_id"]

    deadline = time.time() + 60
    body: dict = {}
    while time.time() < deadline:
        r = client.get(f"/runs/{rid}")
        body = r.json()
        if body.get("status") in _TERMINAL:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"run {rid} did not finish in 60s; last body={body}")

    assert body["status"] == "success", body.get("error")

    event_types = {e["type"] for e in body["events"]}
    for expected in (
        "agent.ready",
        "trust.peers_resolved",
        "trust.established",
        "step.complete",
        "run.complete",
    ):
        assert expected in event_types, f"missing event {expected}: {sorted(event_types)}"

    # Both workflow step ids land in outputs.
    assert "research" in body["outputs"]
    assert "write" in body["outputs"]
