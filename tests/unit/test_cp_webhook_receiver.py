"""Tests for the CP webhook receiver (POST /webhooks/cp/{run_id}).

Covers HMAC verification, append to the run event log, and the
companion GET /runs/{id}/cp-deliveries query endpoint.
"""
from __future__ import annotations

import hmac
import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from aitp_playground.main import create_app


SECRET = "deadbeef" * 4
RUN_ID = "r-test-1"


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    c = TestClient(app)
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


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def test_unknown_run_returns_404(client: TestClient) -> None:
    body = b"{}"
    r = client.post(
        "/webhooks/cp/no-such-run",
        content=body,
        headers={"X-Aitp-Signature": _sig(body)},
    )
    assert r.status_code == 404


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
        "enqueuedAt": "2026-05-26T00:00:00Z",
    }
    body = json.dumps(payload).encode("utf-8")
    r = client.post(
        f"/webhooks/cp/{RUN_ID}",
        content=body,
        headers={
            "X-Aitp-Signature": _sig(body),
            "X-Aitp-Event": "handshake.complete",
            "X-Aitp-Delivery": "d-1",
            "Content-Type": "application/json",
        },
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
