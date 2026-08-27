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

    async def create_webhook(
        self,
        *,
        url: str,
        events: list[str] | None = None,
        secret: Optional[str] = None,
        active: bool = True,
    ) -> Optional[dict[str, Any]]:
        """POST /api/webhooks — subscribe to CP audit events.

        ``events=[]`` (or ``None``) means *all* deliverable event types
        on the CP side. Returns the full created record including the
        webhook ``id`` and ``secret`` (CP autogenerates the secret when
        the caller doesn't supply one). Returns ``None`` when CP is
        disabled or the call failed — callers branch on truthiness.
        """
        if not self.enabled or not url:
            return None
        body: dict[str, Any] = {"url": url, "events": events or [], "active": active}
        if secret:
            body["secret"] = secret
        target = f"{self.settings.cp_base_url.rstrip('/')}/api/webhooks"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(target, json=body, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP create_webhook failed (degraded): %s", exc)
            return None
        if isinstance(data, dict):
            return data
        return None

    async def delete_webhook(self, webhook_id: str) -> bool:
        """DELETE /api/webhooks/{id}. Idempotent — a 404 is treated as success."""
        if not self.enabled or not webhook_id:
            return False
        target = (
            f"{self.settings.cp_base_url.rstrip('/')}/api/webhooks/{webhook_id}"
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.delete(target, headers=self._headers())
                if r.status_code == 404:
                    return True
                r.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP delete_webhook failed (degraded): %s", exc)
            return False

    # ── Observability projections (read-only) ───────────────────────────────

    async def _get_list(
        self, path: str, params: dict[str, Any], key: str
    ) -> list[dict[str, Any]]:
        """Shared GET → list helper. Tolerates either ``{key: [...]}`` or a
        bare top-level list, matching the older fetch_* methods. Returns []
        when CP is disabled or the call fails."""
        if not self.enabled:
            return []
        url = f"{self.settings.cp_base_url.rstrip('/')}{path}"
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params=clean, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP GET %s failed (degraded): %s", path, exc)
            return []
        if isinstance(data, dict):
            return list(data.get(key) or [])
        if isinstance(data, list):
            return data
        return []

    async def fetch_tcts(
        self,
        *,
        issuer: Optional[str] = None,
        subject: Optional[str] = None,
        audience: Optional[str] = None,
        capability: Optional[str] = None,
        session_id: Optional[str] = None,
        active: Optional[bool] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """GET /api/tcts — TCTs the CP has observed (projected from events)."""
        return await self._get_list(
            "/api/tcts",
            {
                "issuer": issuer,
                "subject": subject,
                "audience": audience,
                "capability": capability,
                "sessionId": session_id,
                "active": "true" if active else None,
                "limit": limit,
            },
            "tcts",
        )

    async def fetch_delegations(
        self,
        *,
        root_jti: Optional[str] = None,
        parent_jti: Optional[str] = None,
        delegator: Optional[str] = None,
        delegatee: Optional[str] = None,
        active: Optional[bool] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """GET /api/delegations — delegation chains. ``root_jti`` walks the
        whole descendant tree via the CP's recursive query."""
        return await self._get_list(
            "/api/delegations",
            {
                "root_jti": root_jti,
                "parent_jti": parent_jti,
                "delegator": delegator,
                "delegatee": delegatee,
                "active": "true" if active else None,
                "limit": limit,
            },
            "delegations",
        )

    async def replay_session(
        self,
        session_id: str,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """GET /api/sessions/{id}/replay — ordered event stream for a session."""
        if not session_id:
            return []
        return await self._get_list(
            f"/api/sessions/{session_id}/replay",
            {"since": since, "until": until, "limit": limit},
            "events",
        )

    async def fetch_dashboard_overview(self, window: str = "24h") -> dict[str, Any]:
        """GET /api/dashboard/overview — aggregate counts + recent activity.
        Returns {} when CP is disabled or the call fails."""
        if not self.enabled:
            return {}
        url = f"{self.settings.cp_base_url.rstrip('/')}/api/dashboard/overview"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(
                    url, params={"window": window}, headers=self._headers()
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP fetch_dashboard_overview failed (degraded): %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    async def fetch_dashboard_agents(self) -> list[dict[str, Any]]:
        """GET /api/dashboard/agents — per-agent metrics."""
        return await self._get_list("/api/dashboard/agents", {}, "agents")

    async def list_trust_anchors(
        self, *, namespace: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """GET /api/trust-anchors — configured OIDC issuer bindings."""
        return await self._get_list(
            "/api/trust-anchors", {"namespace": namespace}, "trustAnchors"
        )

    async def list_pinned_keys(
        self, *, namespace: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """GET /api/pinned-keys — registered pinned Ed25519 keys."""
        return await self._get_list(
            "/api/pinned-keys", {"namespace": namespace}, "pinnedKeys"
        )

    # ── Trust configuration (write) ─────────────────────────────────────────

    async def _post_json(
        self, path: str, body: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Shared POST → dict helper. Returns the created/updated record, or
        None when CP is disabled or the call failed."""
        if not self.enabled:
            return None
        url = f"{self.settings.cp_base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=body, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CP POST %s failed (degraded): %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    async def upsert_trust_anchor(
        self,
        *,
        issuer_url: str,
        namespace: Optional[str] = None,
        jwks_url: Optional[str] = None,
        label: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """POST /api/trust-anchors — register an OIDC issuer binding."""
        if not issuer_url:
            return None
        body: dict[str, Any] = {"issuerUrl": issuer_url}
        if namespace:
            body["namespace"] = namespace
        if jwks_url:
            body["jwksUrl"] = jwks_url
        if label:
            body["label"] = label
        return await self._post_json("/api/trust-anchors", body)

    async def upsert_pinned_key(
        self,
        *,
        aid: str,
        pubkey: str,
        namespace: Optional[str] = None,
        label: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """POST /api/pinned-keys — upsert a pinned Ed25519 key for an AID."""
        if not aid or not pubkey:
            return None
        body: dict[str, Any] = {"aid": aid, "pubkey": pubkey}
        if namespace:
            body["namespace"] = namespace
        if label:
            body["label"] = label
        return await self._post_json("/api/pinned-keys", body)
