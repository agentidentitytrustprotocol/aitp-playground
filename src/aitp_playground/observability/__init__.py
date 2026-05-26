"""Observability primitives for the playground.

Pure-Python, no third-party metrics dep — for a demo service the
maintenance cost of pulling in prometheus_client outweighs the value of
the few histograms it would give us. Module exposes a singleton
``Metrics`` instance that records counters / gauges driven by the
event taxonomy emitted from ``runner/context.py``, plus a Prometheus
text-format encoder used by the ``/metrics`` route.
"""
from .metrics import Metrics, metrics, record_event, format_prometheus
from .narrator import narrate_event, narrate_events

__all__ = [
    "Metrics", "metrics", "record_event", "format_prometheus",
    "narrate_event", "narrate_events",
]
