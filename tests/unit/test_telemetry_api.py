"""POST /internal/telemetry — the sink agent subprocesses deliver their
canonical AITP events to. Events must land in the run's event log (where
the CP ingest and SSE stream pick them up)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aitp_playground.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_telemetry_appends_event_to_run_log() -> None:
    c = _client()
    rid = c.post("/runs", json={
        "scenario_ref": "intra-org/trust-gate@1.0.0",
        "inputs": {"topic": "demo"},
    }).json()["run_id"]

    r = c.post("/internal/telemetry", json={
        "run_id": rid,
        "type": "handshake.complete",
        "initiator_aid": "aid-a",
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    events = c.get(f"/runs/{rid}").json()["events"]
    assert any(e.get("type") == "handshake.complete" for e in events)


def test_telemetry_accepts_nested_playground_run_id() -> None:
    """Agents built from the bootstrap file carry the run id under
    ``playground.run_id`` — the sink must honor that shape too."""
    c = _client()
    rid = c.post("/runs", json={
        "scenario_ref": "intra-org/trust-gate@1.0.0",
        "inputs": {"topic": "demo"},
    }).json()["run_id"]

    r = c.post("/internal/telemetry", json={
        "type": "delegation.issued",
        "playground": {"run_id": rid},
    })
    assert r.status_code == 200
    events = c.get(f"/runs/{rid}").json()["events"]
    assert any(e.get("type") == "delegation.issued" for e in events)


def test_telemetry_without_run_id_is_logged_but_not_stored() -> None:
    c = _client()
    store = c.app.state.run_store
    before = set(store.list_ids())
    r = c.post("/internal/telemetry", json={"type": "orphan.event"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert set(store.list_ids()) == before  # no phantom run created
