"""Tests for the CP webhook receiver (POST /webhooks/cp/{run_id}).

Covers HMAC verification (body and signed timestamp), the replay window on
`X-Aitp-Timestamp`, append to the run event log, and the companion
GET /runs/{id}/cp-deliveries query endpoint.
"""
from __future__ import annotations

import hmac
import json
import time
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from tests._auth import AUTH

from aitp_playground.main import create_app


SECRET = "deadbeef" * 4
RUN_ID = "r-test-1"


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    c = TestClient(app, headers=AUTH)
    # Seed a run + the webhook secret on it so the receiver has something
    # to HMAC against.
    app.state.run_store.upsert(RUN_ID, {
        "run_id": RUN_ID,
        "status": "running",
        "scenario_ref": "intra-org/webhook-subscription@1.0.0",
        "outputs": {},
        "events": [],
        "error": None,
        "cp_webhook": {
            "id": "wh-1",
            "secret": SECRET,
            "url": f"http://playground/webhooks/cp/{RUN_ID}",
            "events": [],
        },
    })
    return c


def _now_iso(offset_secs: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset_secs, tz=timezone.utc).isoformat()


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def _headers(body: bytes, ts: int | str | None = None, **extra: str) -> dict[str, str]:
    """Every header CP sends on one attempt, signed now unless `ts` says otherwise."""
    ts = str(int(time.time()) if ts is None else ts)
    return {
        "X-Aitp-Signature": _sig(body),
        "X-Aitp-Timestamp": ts,
        "X-Aitp-Timestamp-Signature": _sig(ts.encode() + b"." + body),
        **extra,
    }


def test_unknown_run_is_gone(client: TestClient) -> None:
    """410, not 404: the CP treats it as "stop delivering here"."""
    body = b"{}"
    r = client.post("/webhooks/cp/no-such-run", content=body, headers=_headers(body))
    assert r.status_code == 410


def test_run_without_subscription_returns_401(client: TestClient) -> None:
    # Seed a run that NEVER subscribed — no cp_webhook block on the record.
    client.app.state.run_store.upsert("r-no-sub", {
        "run_id": "r-no-sub", "status": "running", "events": [],
    })
    body = b'{"eventType":"handshake.complete"}'
    r = client.post(
        "/webhooks/cp/r-no-sub",
        content=body,
        headers={"X-Aitp-Signature": _sig(body)},
    )
    assert r.status_code == 401
    assert "secret" in r.json()["detail"].lower()


def test_missing_signature_header_returns_401(client: TestClient) -> None:
    r = client.post(f"/webhooks/cp/{RUN_ID}", content=b"{}")
    assert r.status_code == 401


def test_bad_signature_returns_401(client: TestClient) -> None:
    body = b'{"eventType":"handshake.complete"}'
    r = client.post(
        f"/webhooks/cp/{RUN_ID}",
        content=body,
        headers={"X-Aitp-Signature": "sha256=" + "0" * 64},
    )
    assert r.status_code == 401


def test_wrong_scheme_returns_401(client: TestClient) -> None:
    body = b"{}"
    r = client.post(
        f"/webhooks/cp/{RUN_ID}",
        content=body,
        headers={"X-Aitp-Signature": "md5=" + hmac.new(SECRET.encode(), body, sha256).hexdigest()},
    )
    assert r.status_code == 401


def test_good_signature_records_event_and_returns_200(client: TestClient) -> None:
    payload = {
        "deliveryId": "d-1",
        "eventType": "handshake.complete",
        "payload": {"type": "handshake.complete", "run_id": RUN_ID, "session_id": "s-1"},
        "enqueuedAt": _now_iso(),
    }
    body = json.dumps(payload).encode("utf-8")
    r = client.post(
        f"/webhooks/cp/{RUN_ID}",
        content=body,
        headers=_headers(
            body, **{
                "X-Aitp-Event": "handshake.complete",
                "X-Aitp-Delivery": "d-1",
                "Content-Type": "application/json",
            },
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    rec = client.app.state.run_store.get(RUN_ID)
    delivered = [e for e in rec["events"] if e["type"] == "cp.webhook.delivered"]
    assert len(delivered) == 1
    assert delivered[0]["delivery_id"] == "d-1"
    assert delivered[0]["event_type"] == "handshake.complete"
    assert delivered[0]["payload"]["eventType"] == "handshake.complete"


def test_cp_deliveries_endpoint_filters_and_strips_secret(client: TestClient) -> None:
    # Inject two deliveries directly so the test isn't coupled to the receiver.
    client.app.state.run_store.append_event(RUN_ID, {
        "type": "cp.webhook.delivered",
        "delivery_id": "d-1",
        "event_type": "handshake.complete",
        "payload": {"foo": "bar"},
    })
    client.app.state.run_store.append_event(RUN_ID, {
        "type": "cp.webhook.delivered",
        "delivery_id": "d-2",
        "event_type": "tct.revoked",
        "payload": {"jti": "j-1"},
    })

    r = client.get(f"/runs/{RUN_ID}/cp-deliveries")
    assert r.status_code == 200
    body = r.json()
    assert body["subscribed"] is True
    assert body["webhook"]["id"] == "wh-1"
    # Secret must be stripped from the response.
    assert "secret" not in body["webhook"]
    assert body["count"] == 2
    assert {d["delivery_id"] for d in body["deliveries"]} == {"d-1", "d-2"}

    r = client.get(f"/runs/{RUN_ID}/cp-deliveries", params={"event_type": "tct.revoked"})
    body = r.json()
    assert body["count"] == 1
    assert body["deliveries"][0]["delivery_id"] == "d-2"


def test_cp_deliveries_for_unknown_run_returns_404(client: TestClient) -> None:
    r = client.get("/runs/no-such-run/cp-deliveries")
    assert r.status_code == 404


def _deliver(client: TestClient, payload: dict, ts: int | str | None = None) -> "object":
    body = json.dumps(payload).encode("utf-8")
    return client.post(
        f"/webhooks/cp/{RUN_ID}",
        content=body,
        headers=_headers(body, ts, **{"Content-Type": "application/json"}),
    )


def _delivered(client: TestClient) -> list[dict]:
    rec = client.app.state.run_store.get(RUN_ID)
    return [e for e in rec["events"] if e["type"] == "cp.webhook.delivered"]


def test_timestamp_signature_matches_the_cp_test_vector() -> None:
    """The CP's published vector, so both sides sign the same bytes."""
    digest = hmac.new(b"topsecret", b'1700000000.{"a":1}', sha256).hexdigest()
    assert digest == "6a939b0c71853d606167625a15168ee9188c6a511c773ef4f42d307f3849e50f"


def test_a_late_retry_with_an_old_enqueued_at_is_accepted(client: TestClient) -> None:
    """A 4th attempt lands ~10.5 min after enqueue. Its signed timestamp is
    fresh, so the old `enqueuedAt` it carries does not matter."""
    r = _deliver(client, {
        "deliveryId": "d-late", "eventType": "tct.revoked", "payload": {},
        "enqueuedAt": _now_iso(-11 * 60),
    })
    assert r.status_code == 200, r.text
    assert [e["delivery_id"] for e in _delivered(client)] == ["d-late"]


# Clear of the boundary: `int(time.time())` already loses up to a second.
@pytest.mark.parametrize("skew", [-310, 310])
def test_a_signed_timestamp_outside_five_minutes_is_rejected(client: TestClient, skew: int) -> None:
    """A captured delivery replayed later: correctly signed, but stale."""
    r = _deliver(client, {
        "deliveryId": "d-old", "eventType": "tct.revoked", "payload": {},
        "enqueuedAt": _now_iso(),
    }, ts=int(time.time()) + skew)
    assert r.status_code == 401
    assert r.json()["detail"] == "stale delivery"
    assert not _delivered(client)


def test_a_body_signature_alone_is_rejected(client: TestClient) -> None:
    body = json.dumps({"deliveryId": "d-x", "enqueuedAt": _now_iso()}).encode()
    r = client.post(f"/webhooks/cp/{RUN_ID}", content=body, headers={"X-Aitp-Signature": _sig(body)})
    assert r.status_code == 401
    assert not _delivered(client)


def test_a_missing_timestamp_signature_is_rejected(client: TestClient) -> None:
    body = b'{"deliveryId":"d-x"}'
    headers = _headers(body)
    del headers["X-Aitp-Timestamp-Signature"]
    r = client.post(f"/webhooks/cp/{RUN_ID}", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "missing X-Aitp-Timestamp-Signature"


def test_a_timestamp_signature_over_another_timestamp_is_rejected(client: TestClient) -> None:
    """Re-stamping a captured delivery without the secret."""
    body = b'{"deliveryId":"d-x"}'
    headers = _headers(body, ts=int(time.time()) - 600)
    headers["X-Aitp-Timestamp"] = str(int(time.time()))
    r = client.post(f"/webhooks/cp/{RUN_ID}", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "bad timestamp signature"


@pytest.mark.parametrize("ts", ["1700000000.5", "-1", "+1700000000", "1e9", "", " 1700000000"])
def test_a_timestamp_that_is_not_bare_digits_is_rejected(client: TestClient, ts: str) -> None:
    """A dotted value would make the `<ts>.<body>` split ambiguous."""
    body = b'{"deliveryId":"d-x"}'
    headers = _headers(body)
    headers["X-Aitp-Timestamp"] = ts
    headers["X-Aitp-Timestamp-Signature"] = _sig(ts.encode() + b"." + body)
    r = client.post(f"/webhooks/cp/{RUN_ID}", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "missing or invalid X-Aitp-Timestamp"


def test_non_json_body_returns_401(client: TestClient) -> None:
    body = b"not json"
    r = client.post(f"/webhooks/cp/{RUN_ID}", content=body, headers=_headers(body))
    assert r.status_code == 401


def test_replayed_delivery_id_is_not_recorded_twice(client: TestClient) -> None:
    payload = {
        "deliveryId": "d-dup", "eventType": "tct.revoked", "payload": {},
        "enqueuedAt": _now_iso(),
    }
    assert _deliver(client, payload).json() == {"ok": True}
    assert _deliver(client, payload).json() == {"ok": True, "duplicate": True}
    rec = client.app.state.run_store.get(RUN_ID)
    assert len([e for e in rec["events"] if e.get("delivery_id") == "d-dup"]) == 1


def test_webhook_route_needs_no_api_key(client: TestClient) -> None:
    """CP holds only the per-webhook HMAC secret, never the API key."""
    bare = TestClient(client.app)
    body = json.dumps({
        "deliveryId": "d-nokey", "eventType": "tct.revoked", "payload": {},
        "enqueuedAt": _now_iso(),
    }).encode("utf-8")
    r = bare.post(
        f"/webhooks/cp/{RUN_ID}", content=body,
        headers=_headers(body, **{"Content-Type": "application/json"}),
    )
    assert r.status_code == 200
