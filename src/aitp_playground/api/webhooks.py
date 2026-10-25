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

Replay protection: every delivery attempt also carries ``X-Aitp-Timestamp``
(Unix seconds, set per attempt) and ``X-Aitp-Timestamp-Signature``, an
HMAC-SHA256 over ``"<timestamp>.<raw body>"`` with the same secret. Both are
required. The timestamp must be bare digits, since a dotted value would make the
``.`` split ambiguous. A delivery whose signed timestamp is more than
``REPLAY_WINDOW_SECS`` from now is refused with 401. The body's ``enqueuedAt`` is
not used for the window: it is fixed at enqueue time, so a late retry
legitimately carries an old one. A body signature alone is never enough, because
it is identical on every retry and would leave replay unbounded. A
``deliveryId`` already recorded on the run is acknowledged without being
appended again.

This route takes no API key: CP holds only the per-webhook HMAC secret.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import time
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..runner.store import RunStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: How far a signed ``X-Aitp-Timestamp`` may sit from now, either way.
REPLAY_WINDOW_SECS = 300

#: Unix seconds as bare ASCII digits. A sign, a dot or an exponent is refused.
_TIMESTAMP_RE = re.compile(r"[0-9]{1,20}")


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


def _hmac_hex(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), data, sha256).hexdigest()


@router.post("/cp/{run_id}")
async def receive_cp_webhook(
    run_id: str,
    request: Request,
    x_aitp_signature: str | None = Header(default=None, alias="X-Aitp-Signature"),
    x_aitp_event: str | None = Header(default=None, alias="X-Aitp-Event"),
    x_aitp_delivery: str | None = Header(default=None, alias="X-Aitp-Delivery"),
    x_aitp_timestamp: str | None = Header(default=None, alias="X-Aitp-Timestamp"),
    x_aitp_timestamp_signature: str | None = Header(
        default=None, alias="X-Aitp-Timestamp-Signature",
    ),
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
    if not _constant_time_eq(sig, _hmac_hex(secret, raw)):
        raise HTTPException(status_code=401, detail="bad signature")

    if x_aitp_timestamp is None or not _TIMESTAMP_RE.fullmatch(x_aitp_timestamp):
        raise HTTPException(status_code=401, detail="missing or invalid X-Aitp-Timestamp")
    ts_sig = _parse_signature_header(x_aitp_timestamp_signature)
    if ts_sig is None:
        raise HTTPException(status_code=401, detail="missing X-Aitp-Timestamp-Signature")
    signed = x_aitp_timestamp.encode("ascii") + b"." + raw
    if not _constant_time_eq(ts_sig, _hmac_hex(secret, signed)):
        raise HTTPException(status_code=401, detail="bad timestamp signature")
    now = time.time()
    if abs(now - int(x_aitp_timestamp)) > REPLAY_WINDOW_SECS:
        raise HTTPException(status_code=401, detail="stale delivery")

    # Only a verified, fresh body is parsed. CP always sends its JSON
    # delivery envelope; anything else is refused rather than recorded.
    try:
        body_obj = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=401, detail="delivery body is not JSON") from None
    if not isinstance(body_obj, dict):
        raise HTTPException(status_code=401, detail="delivery body is not a JSON object")

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
