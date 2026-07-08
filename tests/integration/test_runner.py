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


def test_cancel_inflight_run_reaches_terminal_state() -> None:
    """Cancel a run while its agents are up. The cancel endpoint kills the
    subprocesses and marks the run cancelled; the background task then fails
    on its next inter-agent call and finalizes. Either terminal outcome is
    acceptable — what must NOT happen is the run hanging or succeeding.

    TestClient executes FastAPI background tasks synchronously (the POST
    would only return after the run finished), so the POST runs on a side
    thread while this thread observes and cancels mid-flight."""
    import threading

    client = TestClient(create_app())
    before = {r["run_id"] for r in client.get("/runs").json()["runs"]}

    poster = threading.Thread(
        target=lambda: client.post("/runs", json={
            "scenario_ref": "intra-org/research-and-write@1.0.0",
            "inputs": {"topic": "cancel me"},
        }),
        daemon=True,
    )
    poster.start()

    # Discover the new run_id from the store-backed listing.
    deadline = time.time() + 15
    rid = None
    while time.time() < deadline and rid is None:
        new = {r["run_id"] for r in client.get("/runs").json()["runs"]} - before
        if new:
            rid = new.pop()
        else:
            time.sleep(0.1)
    assert rid, "run did not appear in /runs within 15s"

    # Wait until at least one agent is ready so the cancel lands mid-flight.
    deadline = time.time() + 45
    while time.time() < deadline:
        body = client.get(f"/runs/{rid}").json()
        if any(e["type"] == "agent.ready" for e in body.get("events", [])):
            break
        if body.get("status") in _TERMINAL:
            pytest.fail(f"run went terminal before cancel: {body.get('status')}")
        time.sleep(0.2)
    else:
        pytest.fail("no agent became ready within 45s")

    cancelled = client.post(f"/runs/{rid}/cancel")
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["cancelled"] is True

    deadline = time.time() + 30
    status = None
    while time.time() < deadline:
        status = client.get(f"/runs/{rid}/status").json()["status"]
        if status in _TERMINAL:
            break
        time.sleep(0.5)
    assert status in {"cancelled", "failed"}, status

    # Cancelling a terminal run is a no-op, not an error.
    again = client.post(f"/runs/{rid}/cancel")
    assert again.status_code == 202
    assert again.json()["cancelled"] is False
    assert again.json()["reason"] == "already terminal"

    poster.join(timeout=60)
    assert not poster.is_alive(), "background run thread did not finish"


def test_run_with_invalid_inputs_fails_fast_without_spawning() -> None:
    """Schema-invalid inputs must terminate the run before any agent
    subprocess is spawned."""
    client = TestClient(create_app())
    posted = client.post("/runs", json={
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "inputs": {"topic": 12345},  # schema requires a string
    })
    assert posted.status_code == 202, posted.text
    rid = posted.json()["run_id"]

    deadline = time.time() + 15
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/runs/{rid}").json()
        if body.get("status") in _TERMINAL:
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"run {rid} did not finish in 15s; last body={body}")

    assert body["status"] == "failed"
    assert "inputs validation" in (body.get("error") or "")
    event_types = {e["type"] for e in body["events"]}
    assert "agent.spawning" not in event_types
