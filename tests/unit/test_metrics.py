"""Tests for the in-process metrics registry + /metrics endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aitp_playground.main import create_app
from aitp_playground.observability.metrics import (
    format_prometheus,
    metrics,
    record_event,
)


@pytest.fixture(autouse=True)
def _clean_metrics() -> None:
    """Reset module-level counters between tests so order doesn't matter."""
    metrics.reset()
    # Re-register so the static schema is present even after reset.
    from aitp_playground.observability import metrics as _m  # noqa: F401


def test_record_event_run_lifecycle_increments_runs_total() -> None:
    record_event({"type": "run.started", "run_id": "r1"})
    record_event({"type": "run.complete", "run_id": "r1"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_runs_total", (("status", "success"),))] == 1.0
    # Active gauge returns to zero on completion.
    assert snap["gauges"][("aitp_playground_runs_active", ())] == 0.0


def test_record_event_run_failed_counts_as_failed() -> None:
    record_event({"type": "run.started", "run_id": "r"})
    record_event({"type": "run.failed", "run_id": "r"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_runs_total", (("status", "failed"),))] == 1.0


def test_record_event_run_cancelled_counts_as_cancelled_and_decrements_once() -> None:
    """`run.cancelled` is emitted directly by `api/runs.py`'s cancel route
    (no `run.failed` follow-up — `runner/engine.py`'s `_finalize_failure`
    guards against that double-emit). One run must land in exactly one
    `runs_total` label and decrement `runs_active` exactly once — if the
    guard elsewhere ever regresses and both events fire for the same run,
    `runs_active` goes negative before the `max(0.0, ...)` floor, which is
    itself a symptom worth surfacing, not something to silently tolerate here.
    """
    record_event({"type": "run.started", "run_id": "r"})
    record_event({"type": "run.cancelled", "run_id": "r"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_runs_total", (("status", "cancelled"),))] == 1.0
    assert snap["counters"].get(("aitp_playground_runs_total", (("status", "failed"),))) is None
    assert snap["gauges"][("aitp_playground_runs_active", ())] == 0.0


def test_record_event_handshake_and_tct() -> None:
    record_event({"type": "trust.established"})
    record_event({"type": "trust.established"})
    record_event({"type": "handshake.failed"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_handshakes_total", (("outcome", "established"),))] == 2.0
    assert snap["counters"][("aitp_playground_handshakes_total", (("outcome", "failed"),))] == 1.0
    assert snap["counters"][("aitp_playground_tcts_issued_total", ())] == 2.0


def test_record_event_delegation_outcomes() -> None:
    record_event({"type": "delegation.issued"})
    record_event({"type": "delegation.redeemed"})
    record_event({"type": "delegation.rejected"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_delegations_total", (("outcome", "issued"),))] == 1.0
    assert snap["counters"][("aitp_playground_delegations_total", (("outcome", "redeemed"),))] == 1.0
    assert snap["counters"][("aitp_playground_delegations_total", (("outcome", "rejected"),))] == 1.0
    # A redemption also issues a fresh TCT.
    assert snap["counters"][("aitp_playground_tcts_issued_total", ())] == 1.0


def test_record_event_revocation_source_labels() -> None:
    record_event({"type": "tct.revoked"})
    record_event({"type": "revocation.published"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_revocations_total", (("source", "local"),))] == 1.0
    assert snap["counters"][("aitp_playground_revocations_total", (("source", "cp"),))] == 1.0


def test_record_event_key_rotation() -> None:
    record_event({"type": "identity.key.rotated"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_key_rotations_total", ())] == 1.0


def test_record_event_capability_call_outcomes() -> None:
    # A step.complete with a capability is a successful capability call.
    record_event({"type": "step.complete", "capability": "research.query"})
    record_event({"type": "step.complete", "capability": None})  # not a cap call
    record_event({"type": "step.access_denied", "capability": "research.query"})
    snap = metrics.snapshot()
    assert snap["counters"][("aitp_playground_capability_calls_total", (("outcome", "success"),))] == 1.0
    assert snap["counters"][("aitp_playground_capability_calls_total", (("outcome", "denied"),))] == 1.0


def test_record_event_unknown_type_is_ignored() -> None:
    record_event({"type": "totally.made.up"})
    record_event({})  # no type
    # No assertion error means the unknown event didn't blow up; counters
    # remain at their declared zero baseline.
    snap = metrics.snapshot()
    # Sanity: a counter we never touched stays at zero.
    assert snap["counters"].get(
        ("aitp_playground_handshakes_total", (("outcome", "established"),))
    ) is None


def test_format_prometheus_produces_help_and_type_lines() -> None:
    record_event({"type": "trust.established"})
    out = format_prometheus()
    assert "# HELP aitp_playground_handshakes_total " in out
    assert "# TYPE aitp_playground_handshakes_total counter" in out
    assert 'aitp_playground_handshakes_total{outcome="established"} 1.0' in out


def test_format_prometheus_escapes_label_values() -> None:
    metrics.inc("aitp_playground_test_metric", {"path": 'a"b\\c\n'})
    out = format_prometheus()
    assert 'path="a\\"b\\\\c\\n"' in out


def test_metrics_endpoint_returns_text() -> None:
    client = TestClient(create_app())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # Static schema is present even with no observations recorded.
    assert "aitp_playground_runs_total" in r.text
    assert "aitp_playground_handshakes_total" in r.text
