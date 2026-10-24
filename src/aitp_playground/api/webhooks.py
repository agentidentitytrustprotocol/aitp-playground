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

Replay protection: CP sends no separate timestamp header, but the signed
body carries ``enqueuedAt`` (ISO-8601, fixed at enqueue time) and
``deliveryId``. A delivery whose signed ``enqueuedAt`` is more than
``REPLAY_WINDOW_SECS`` from now is refused with 401, and a ``deliveryId``
already recorded on the run is acknowledged without being appended again.
CP's default retry schedule (attempts at +0s, +30s, +2.5m) fits inside the
window; a later retry is refused like any other stale delivery.

This route takes no API key: CP holds only the per-webhook HMAC secret.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from datetime import datetime
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..runner.store import RunStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: How far a signed ``enqueuedAt`` may sit from now, either way.
REPLAY_WINDOW_SECS = 300


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


def _enqueued_at(body: object) -> float | None:
    if not isinstance(body, dict) or not isinstance(body.get("enqueuedAt"), str):
        return None
    try:
        return datetime.fromisoformat(body["enqueuedAt"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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

    # Only a verified body is parsed. CP always sends its JSON delivery
    # envelope; anything else cannot carry a signed timestamp, so it is
    # refused rather than recorded.
    try:
        body_obj = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=401, detail="delivery body is not JSON") from None
    enqueued = _enqueued_at(body_obj)
    if enqueued is None:
        raise HTTPException(status_code=401, detail="missing or invalid enqueuedAt")
    now = time.time()
    if abs(now - enqueued) > REPLAY_WINDOW_SECS:
        raise HTTPException(status_code=401, detail="stale delivery")

    delivery_id = body_obj.get("deliveryId") or x_aitp_delivery
    if delivery_id and any(
        e.get("type") == "cp.webhook.delivered" and e.get("delivery_id") == delivery_id
        for e in (record.get("events") or [])
    ):
        return {"ok": True, "duplicate": True}

    store.append_event(run_id, {
        "type": "cp.webhook.delivered",
        "ts": now,
        "run_id": run_id,
        "delivery_id": delivery_id,
        "event_type": x_aitp_event or body_obj.get("eventType"),
        "payload": body_obj,
    })
    return {"ok": True}
