"""Webhook receiver for Control Plane fan-out.

CP delivers ``POST /webhooks/cp/{run_id}`` with the body bytes captured
at enqueue time and ``X-Aitp-Signature: sha256=<hex>`` computed from
those bytes using the secret CP generated when the webhook was
created. The playground stores the secret on the run record when it
subscribes; this route verifies the HMAC, then appends the delivery to
the run's event log as ``cp.webhook.delivered`` so callers see it via
SSE / ``GET /runs/{id}`` / the narrator.

Unverified deliveries return 401 and are NOT appended — they don't
become observable run history. CP retries on non-2xx, so transient
verification failures are safe.
"""
from __future__ import annotations

import hmac
import logging
import time
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..runner.store import RunStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _store(request: Request) -> RunStore:
    return request.app.state.run_store


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _parse_signature_header(value: str | None) -> str | None:
    """CP sends 'sha256=<hex>'. Tolerate either form so we don't 401 on
    a future format tweak — we still require a non-empty hex tail."""
    if not value:
        return None
    if "=" in value:
        scheme, _, sig = value.partition("=")
        if scheme.strip().lower() != "sha256":
            return None
        return sig.strip() or None
    return value.strip() or None


@router.post("/cp/{run_id}")
async def receive_cp_webhook(
    run_id: str,
    request: Request,
    x_aitp_signature: str | None = Header(default=None, alias="X-Aitp-Signature"),
    x_aitp_event: str | None = Header(default=None, alias="X-Aitp-Event"),
    x_aitp_delivery: str | None = Header(default=None, alias="X-Aitp-Delivery"),
) -> dict[str, Any]:
    store = _store(request)
    record = store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    cp_webhook = record.get("cp_webhook") or {}
    secret: str | None = cp_webhook.get("secret")
    if not secret:
        # The run never subscribed (or the secret didn't land). Reject so
        # CP marks the delivery as failed rather than silently dropping.
        raise HTTPException(status_code=401, detail="no webhook secret on file")

    raw = await request.body()
    sig = _parse_signature_header(x_aitp_signature)
    if sig is None:
        raise HTTPException(status_code=401, detail="missing X-Aitp-Signature")
    expected = hmac.new(secret.encode("utf-8"), raw, sha256).hexdigest()
    if not _constant_time_eq(sig, expected):
        raise HTTPException(status_code=401, detail="bad signature")

    # Body is JSON per CP convention. We tolerate non-JSON for robustness
    # (still recorded as raw text), but the normal path is the structured
    # delivery envelope CP sends.
    try:
        import json as _json
        body_obj = _json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:  # noqa: BLE001
        body_obj = {"raw": raw.decode("utf-8", errors="replace")}

    store.append_event(run_id, {
        "type": "cp.webhook.delivered",
        "ts": time.time(),
        "run_id": run_id,
        "delivery_id": x_aitp_delivery,
        "event_type": x_aitp_event or (body_obj.get("eventType") if isinstance(body_obj, dict) else None),
        "payload": body_obj,
    })
    return {"ok": True}
