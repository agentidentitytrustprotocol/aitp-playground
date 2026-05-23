"""POST events to playground /internal/telemetry — best-effort, never raises."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def emit_event(event_type: str, bootstrap: dict[str, Any], **fields: Any) -> None:
    url = bootstrap.get("playground", {}).get("telemetry_url")
    payload = {
        "type": event_type,
        "run_id": bootstrap.get("run_id"),
        "agent_id": bootstrap.get("agent_id"),
        "ts": time.time(),
        **fields,
    }
    if not url:
        logger.debug("telemetry (no url) %s", payload)
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry post failed (ignored): %s", exc)
