"""Minimal Control Plane client. Every call gracefully degrades to a no-op."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)


class CpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.cp_base_url)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.cp_api_key:
            h["Authorization"] = f"Bearer {self.settings.cp_api_key}"
        return h

    @property
    def _timeout(self) -> float:
        return self.settings.cp_timeout_ms / 1000

    async def discover_by_capability(self, capability: str) -> list[dict[str, Any]]:
        """GET /registry/agents?capability=... — returns [] if CP disabled or fails."""
        if not self.enabled:
            return []
        url = f"{self.settings.cp_base_url.rstrip('/')}/registry/agents"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params={"capability": capability}, headers=self._headers())
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    return list(data.get("agents", []))
                if isinstance(data, list):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP discover_by_capability failed (degraded): %s", exc)
        return []

    async def ingest_events(self, events: list[Any]) -> None:
        """POST /events — fire-and-forget."""
        if not self.enabled or not events:
            return
        url = f"{self.settings.cp_base_url.rstrip('/')}/events"
        payload = {"events": [e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in events]}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(url, json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP ingest_events failed (degraded): %s", exc)
