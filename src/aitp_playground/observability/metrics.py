"""In-process metrics collection.

Counters and gauges are keyed by ``(name, labels)``. Labels are a
sorted-tuple of ``(key, value)`` pairs so dict order doesn't break key
equality. There's no histogram support — for a demo service tracking
rates and totals is enough and the Prometheus exposition format we emit
is just counters/gauges.

The ``Metrics`` instance is module-level. Tests can call
``metrics.reset()`` between cases to avoid cross-test contamination.
"""
from __future__ import annotations

import threading
from typing import Any, Mapping


Labels = tuple[tuple[str, str], ...]


def _norm(labels: Mapping[str, str] | None) -> Labels:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class Metrics:
    """Thread-safe counter + gauge registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, Labels], float] = {}
        self._gauges: dict[tuple[str, Labels], float] = {}
        # Help text registered the first time a metric is touched.
        self._help: dict[str, str] = {}

    def reset(self) -> None:
        """Clear all observations but keep the registered-metric baseline.

        The static schema (one zero-valued empty-labels series per
        registered name) is restored from ``self._help`` so /metrics
        still returns the expected catalog after a reset — tests rely on
        this behavior.
        """
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            for name in self._help:
                self._counters[(name, ())] = 0.0

    def register(self, name: str, help_text: str) -> None:
        """Idempotently register a metric's help text. Safe to call even
        if no observation has been recorded yet — the metric will appear
        in the exposition with a zero value for the empty-labels series.
        """
        with self._lock:
            self._help.setdefault(name, help_text)
            self._counters.setdefault((name, ()), 0.0)

    def inc(self, name: str, labels: Mapping[str, str] | None = None, amount: float = 1.0) -> None:
        key = (name, _norm(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def set(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        key = (name, _norm(labels))
        with self._lock:
            self._gauges[key] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "help": dict(self._help),
            }


metrics = Metrics()


# ── Metric catalog ──────────────────────────────────────────────────────────
# Registered up-front so /metrics returns a stable schema even before the
# first run. The names are flat, lower_snake_case, and prefixed with
# ``aitp_playground_`` per Prometheus naming conventions.

_RUNS_TOTAL = "aitp_playground_runs_total"
_HANDSHAKES_TOTAL = "aitp_playground_handshakes_total"
_TCTS_ISSUED_TOTAL = "aitp_playground_tcts_issued_total"
_DELEGATIONS_TOTAL = "aitp_playground_delegations_total"
_REVOCATIONS_TOTAL = "aitp_playground_revocations_total"
_KEY_ROTATIONS_TOTAL = "aitp_playground_key_rotations_total"
_CAPABILITY_CALLS_TOTAL = "aitp_playground_capability_calls_total"
_STEP_OUTCOMES_TOTAL = "aitp_playground_step_outcomes_total"
_RUNS_ACTIVE = "aitp_playground_runs_active"

metrics.register(_RUNS_TOTAL, "Scenario runs by terminal status")
metrics.register(_HANDSHAKES_TOTAL, "AITP handshakes by outcome")
metrics.register(_TCTS_ISSUED_TOTAL, "Trust Context Tokens issued (any direction)")
metrics.register(_DELEGATIONS_TOTAL, "Delegation operations by outcome")
metrics.register(_REVOCATIONS_TOTAL, "TCT revocations by propagation source")
metrics.register(_KEY_ROTATIONS_TOTAL, "Agent key rotations")
metrics.register(_CAPABILITY_CALLS_TOTAL, "Capability invocations by outcome")
metrics.register(_STEP_OUTCOMES_TOTAL, "Workflow steps by type and outcome")
metrics.register(_RUNS_ACTIVE, "Scenario runs currently executing")


# ── Event → metric mapping ──────────────────────────────────────────────────


def record_event(event: Mapping[str, Any]) -> None:
    """Translate a single ``RunEvent``-shaped dict into metric updates.

    Centralized here so the engine can stay event-emission focused — the
    only coupling is one call from ``RunContext.emit``. Unknown event
    types are silently ignored; adding a new event taxonomy entry does
    not require touching the engine.
    """
    etype = event.get("type") or ""
    if not etype:
        return

    if etype == "run.started":
        # Gauge increment via a manual read; counters can also serve as
        # "active" but the convention is gauge for a current count.
        snap = metrics.snapshot()
        cur = snap["gauges"].get((_RUNS_ACTIVE, ()), 0.0)
        metrics.set(_RUNS_ACTIVE, cur + 1)
        return
    if etype in ("run.complete", "run.failed", "run.cancelled"):
        snap = metrics.snapshot()
        cur = snap["gauges"].get((_RUNS_ACTIVE, ()), 0.0)
        metrics.set(_RUNS_ACTIVE, max(0.0, cur - 1))
        status = etype.split(".", 1)[1]  # "complete" -> "complete" etc.
        # Normalize complete -> success so the labels read naturally.
        if status == "complete":
            status = "success"
        metrics.inc(_RUNS_TOTAL, {"status": status})
        return

    if etype == "trust.established":
        metrics.inc(_HANDSHAKES_TOTAL, {"outcome": "established"})
        metrics.inc(_TCTS_ISSUED_TOTAL)
        return
    if etype in ("handshake.failed", "trust.failed"):
        metrics.inc(_HANDSHAKES_TOTAL, {"outcome": "failed"})
        return

    if etype == "delegation.issued":
        metrics.inc(_DELEGATIONS_TOTAL, {"outcome": "issued"})
        return
    if etype == "delegation.redeemed":
        metrics.inc(_DELEGATIONS_TOTAL, {"outcome": "redeemed"})
        # A redemption also produces a fresh TCT bound to the delegatee.
        metrics.inc(_TCTS_ISSUED_TOTAL)
        return
    if etype == "delegation.rejected":
        metrics.inc(_DELEGATIONS_TOTAL, {"outcome": "rejected"})
        return

    if etype == "tct.revoked":
        metrics.inc(_REVOCATIONS_TOTAL, {"source": "local"})
        return
    if etype == "revocation.published":
        metrics.inc(_REVOCATIONS_TOTAL, {"source": "cp"})
        return

    if etype == "identity.key.rotated":
        metrics.inc(_KEY_ROTATIONS_TOTAL)
        return

    if etype == "step.complete":
        metrics.inc(_STEP_OUTCOMES_TOTAL, {"outcome": "complete"})
        # If the step is a capability call (step has capability), also
        # bump the capability success counter.
        if event.get("capability"):
            metrics.inc(_CAPABILITY_CALLS_TOTAL, {"outcome": "success"})
        return
    if etype in ("step.access_denied", "step.unexpected_status"):
        metrics.inc(_STEP_OUTCOMES_TOTAL, {"outcome": "denied"})
        if event.get("capability"):
            metrics.inc(_CAPABILITY_CALLS_TOTAL, {"outcome": "denied"})
        return
    if etype == "step.skipped":
        metrics.inc(_STEP_OUTCOMES_TOTAL, {"outcome": "skipped"})
        return


# ── Prometheus text-format encoder ──────────────────────────────────────────


def _escape(value: str) -> str:
    # Prometheus label value escaping: \\, \", \n.
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_prometheus() -> str:
    """Render the current metrics in Prometheus text exposition format.

    Output ordering is deterministic so test assertions can match exact
    strings without sorting tricks.
    """
    snap = metrics.snapshot()
    lines: list[str] = []
    # Merge counters + gauges by metric name so each name's HELP/TYPE pair
    # appears once. Counter is the default; gauges are differentiated by
    # the gauge-keyed snapshot.
    names: dict[str, str] = {}
    for (name, _labels), _ in snap["counters"].items():
        names.setdefault(name, "counter")
    for (name, _labels), _ in snap["gauges"].items():
        # A gauge supersedes a counter declaration for the same name —
        # we don't currently mix, but be explicit.
        names[name] = "gauge"

    for name in sorted(names.keys()):
        help_text = snap["help"].get(name, "")
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {names[name]}")
        series = []
        if names[name] == "counter":
            for (n, labels), v in snap["counters"].items():
                if n == name:
                    series.append((labels, v))
        else:
            for (n, labels), v in snap["gauges"].items():
                if n == name:
                    series.append((labels, v))
        # Stable order: sort by labels.
        for labels, value in sorted(series, key=lambda p: p[0]):
            if labels:
                label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                lines.append(f"{name}{{{label_str}}} {value}")
            else:
                lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
