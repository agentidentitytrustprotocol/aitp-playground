"""Prometheus-style /metrics endpoint.

Plain text exposition, no auth — matches the convention of every other
Prometheus-scraped service. Counters are driven by the event taxonomy
emitted from the scenario runner via ``observability.metrics.record_event``.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from ..observability.metrics import format_prometheus

router = APIRouter(tags=["observability"])


@router.get("/metrics")
def metrics_endpoint() -> Response:
    return Response(
        format_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
