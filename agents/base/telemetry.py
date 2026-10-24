"""POST events to playground /internal/telemetry — best-effort, never raises."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def emit_event(event_type: str, bootstrap: dict[str, Any], **fields: Any) -> None:
    pg = bootstrap.get("playground", {})
    url = pg.get("telemetry_url")
    # The playground's /internal/telemetry requires the per-process token it
    # wrote into this bootstrap; without it the post is refused (and ignored).
    token = pg.get("telemetry_token")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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
            await client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.debug("telemetry post failed (ignored): %s", exc)
