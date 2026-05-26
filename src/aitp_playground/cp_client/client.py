"""Minimal Control Plane client. Every call gracefully degrades to a no-op."""
from __future__ import annotations

import logging
from typing import Any, Optional

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
        """GET /api/registry/agents?capability=... — returns [] if CP disabled or fails."""
        if not self.enabled:
            return []
        url = f"{self.settings.cp_base_url.rstrip('/')}/api/registry/agents"
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
        """POST /api/events — fire-and-forget."""
        if not self.enabled or not events:
            return
        url = f"{self.settings.cp_base_url.rstrip('/')}/api/events"
        payload = {"events": [e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in events]}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(url, json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP ingest_events failed (degraded): %s", exc)

    async def publish_revocation(self, jti: str, reason: str = "") -> bool:
        """POST /api/revocation/entries — record a TCT jti as revoked.

        Returns True on a 2xx, False when CP is disabled or the call failed.
        Idempotent on the CP side; re-posting an existing jti is a no-op.
        """
        if not self.enabled or not jti:
            return False
        url = f"{self.settings.cp_base_url.rstrip('/')}/api/revocation/entries"
        body: dict[str, Any] = {"jti": jti}
        if reason:
            body["reason"] = reason
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=body, headers=self._headers())
                r.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP publish_revocation failed (degraded): %s", exc)
            return False

    async def fetch_events_history(
        self,
        *,
        run_id: Optional[str] = None,
        aid: Optional[str] = None,
        type_: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """GET /api/events/history — query CP audit events.

        Filter parameters mirror the CP route's query string. Returns
        the raw list of event dicts (or [] when CP is disabled or the
        call fails); callers shape the response.
        """
        if not self.enabled:
            return []
        url = f"{self.settings.cp_base_url.rstrip('/')}/api/events/history"
        params: dict[str, Any] = {"limit": limit}
        if run_id:
            params["run_id"] = run_id
        if aid:
            params["aid"] = aid
        if type_:
            params["type"] = type_
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP fetch_events_history failed (degraded): %s", exc)
            return []
        if isinstance(data, dict):
            return list(data.get("events") or [])
        if isinstance(data, list):
            return data
        return []

    async def fetch_sessions(
        self,
        *,
        run_id: Optional[str] = None,
        aid: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """GET /api/sessions — query CP handshake-session records."""
        if not self.enabled:
            return []
        url = f"{self.settings.cp_base_url.rstrip('/')}/api/sessions"
        params: dict[str, Any] = {"limit": limit}
        if run_id:
            params["run_id"] = run_id
        if aid:
            params["aid"] = aid
        if status:
            params["status"] = status
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params=params, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP fetch_sessions failed (degraded): %s", exc)
            return []
        if isinstance(data, dict):
            return list(data.get("sessions") or [])
        if isinstance(data, list):
            return data
        return []

    async def fetch_revocation_list(self) -> list[str]:
        """GET /.well-known/aitp-revocation-list — return the list of revoked
        jtis from the signed envelope. Returns [] when CP is disabled or the
        call failed.

        The well-known endpoint is public on the CP; no bearer header is
        sent. The signed-envelope body has the shape
        ``{"revocation_list": {"entries": [{"jti": ...}, ...]}}`` — we
        extract just the jtis since the playground's local deny-set is
        keyed on jti.
        """
        if not self.enabled:
            return []
        url = f"{self.settings.cp_base_url.rstrip('/')}/.well-known/aitp-revocation-list"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP fetch_revocation_list failed (degraded): %s", exc)
            return []
        # Tolerate either {"entries": [...]} at the root or a wrapping
        # envelope; CP's exact field shape may evolve, so probe both.
        entries: list[Any] = []
        if isinstance(data, dict):
            inner = data.get("revocation_list") or data
            if isinstance(inner, dict):
                entries = list(inner.get("entries") or [])
        return [
            (e.get("jti") if isinstance(e, dict) else str(e))
            for e in entries
            if (isinstance(e, dict) and e.get("jti")) or isinstance(e, str)
        ]
