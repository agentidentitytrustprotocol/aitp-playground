"""Prometheus-style /metrics endpoint.

Plain text exposition behind the API key like every other non-static
route (a Prometheus scraper sends it as ``bearer_token``). Counters are driven by the event taxonomy
emitted from the scenario runner via ``observability.metrics.record_event``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ..observability.metrics import format_prometheus
from ._deps import require_api_key

router = APIRouter(tags=["observability"])


@router.get("/metrics", dependencies=[Depends(require_api_key)])
def metrics_endpoint() -> Response:
    return Response(
        format_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
