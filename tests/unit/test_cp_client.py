"""CpClient HTTP method-level tests.

The full CP HTTP contract is exercised by tests/integration/test_protocol_e2e.py
against the live container in docker-compose.test.yml. These tests stand
in for the offline path: shapes, headers, graceful-degradation when CP is
disabled, and tolerant parsing of the revocation-list envelope.

httpx.MockTransport sidesteps any need for respx/httpx_mock as a test dep.
"""
from __future__ import annotations

import httpx
import pytest

from aitp_playground.config import Settings
from aitp_playground.cp_client.client import CpClient


def _client(transport: httpx.MockTransport, *, base_url: str = "http://cp", api_key: str = "") -> CpClient:
    """Build a CpClient that routes every async httpx call through ``transport``.

    Patching httpx.AsyncClient via monkeypatch in each test is verbose; we
    inject the transport once and let httpx wire it through.
    """
    settings = Settings(cp_base_url=base_url, cp_api_key=api_key, cp_timeout_ms=1000)
    cp = CpClient(settings)
    cp._test_transport = transport  # type: ignore[attr-defined]
    return cp


@pytest.fixture(autouse=True)
def patch_async_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``httpx.AsyncClient(...)`` inside the CpClient pick up
    the per-test MockTransport if one was attached via _test_transport.

    We do this once via a wrapping fixture so individual tests stay tight.
    """
    real_ctor = httpx.AsyncClient

    def _wrapper(*args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        # The CpClient stores the transport on the singleton instance, but
        # the client is the caller of httpx.AsyncClient(...). There's no
        # easy hand-off — instead we read the most-recently-set transport
        # off a module attribute the helper below populates.
        t = getattr(patch_async_client, "_transport", None)
        if t is not None:
            kwargs.setdefault("transport", t)
        return real_ctor(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _wrapper)


def _set_transport(t: httpx.MockTransport) -> None:
    patch_async_client._transport = t  # type: ignore[attr-defined]


def _clear_transport() -> None:
    patch_async_client._transport = None  # type: ignore[attr-defined]


# ── publish_revocation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_revocation_disabled_returns_false() -> None:
    cp = CpClient(Settings(cp_base_url="", cp_api_key=""))
    assert await cp.publish_revocation("jti-x", reason="r") is False


@pytest.mark.asyncio
async def test_publish_revocation_posts_jti_and_reason() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"jti": "jti-x", "revokedAt": "2026-05-25T00:00:00Z"})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler), api_key="test-key")
        ok = await cp.publish_revocation("jti-x", reason="compromised")
    finally:
        _clear_transport()
    assert ok is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/revocation/entries")
    assert captured["auth"] == "Bearer test-key"
    assert '"jti":"jti-x"' in captured["body"]
    assert '"reason":"compromised"' in captured["body"]


@pytest.mark.asyncio
async def test_publish_revocation_omits_reason_when_blank() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"jti": "jti-y"})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        ok = await cp.publish_revocation("jti-y")
    finally:
        _clear_transport()
    assert ok is True
    assert "reason" not in captured["body"]


@pytest.mark.asyncio
async def test_publish_revocation_degrades_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        ok = await cp.publish_revocation("jti-z")
    finally:
        _clear_transport()
    assert ok is False


# ── create_webhook / delete_webhook ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_webhook_disabled_returns_none() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.create_webhook(url="http://playground/webhooks/cp/r1") is None


@pytest.mark.asyncio
async def test_create_webhook_posts_body_and_returns_secret() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={
            "id": "wh-1",
            "secret": "deadbeef" * 4,
            "url": "http://playground/webhooks/cp/r1",
            "events": ["handshake.complete"],
            "active": True,
            "createdAt": "2026-05-26T00:00:00Z",
        })

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler), api_key="key")
        created = await cp.create_webhook(
            url="http://playground/webhooks/cp/r1",
            events=["handshake.complete"],
        )
    finally:
        _clear_transport()
    assert created is not None
    assert created["id"] == "wh-1"
    assert created["secret"].startswith("deadbeef")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/webhooks")
    assert captured["auth"] == "Bearer key"
    assert '"url":"http://playground/webhooks/cp/r1"' in captured["body"]
    assert '"events":["handshake.complete"]' in captured["body"]


@pytest.mark.asyncio
async def test_create_webhook_degrades_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.create_webhook(url="http://playground/webhooks/cp/r1")
    finally:
        _clear_transport()
    assert out is None


@pytest.mark.asyncio
async def test_delete_webhook_treats_404_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert str(request.url).endswith("/api/webhooks/wh-1")
        return httpx.Response(404)

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        ok = await cp.delete_webhook("wh-1")
    finally:
        _clear_transport()
    assert ok is True


@pytest.mark.asyncio
async def test_delete_webhook_disabled_returns_false() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.delete_webhook("wh-1") is False


# ── fetch_events_history ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_events_history_disabled_returns_empty() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.fetch_events_history(run_id="r1") == []


@pytest.mark.asyncio
async def test_fetch_events_history_passes_filters_and_unwraps_envelope() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"events": [
            {"type": "run.complete", "run_id": "r1"},
            {"type": "trust.established", "run_id": "r1"},
        ]})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler), api_key="k")
        events = await cp.fetch_events_history(
            run_id="r1", aid="aid:pubkey:x", type_="run.complete", limit=50,
        )
    finally:
        _clear_transport()
    assert "run_id=r1" in captured["url"]
    assert "aid=aid%3Apubkey%3Ax" in captured["url"]  # url-encoded colon
    assert "type=run.complete" in captured["url"]
    assert "limit=50" in captured["url"]
    assert len(events) == 2


# ── fetch_sessions ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_sessions_unwraps_envelope_and_passes_status() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"sessions": [
            {"session_id": "s1", "status": "completed", "run_id": "r1"},
        ]})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        sessions = await cp.fetch_sessions(run_id="r1", status="completed")
    finally:
        _clear_transport()
    assert "status=completed" in captured["url"]
    assert sessions[0]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_fetch_sessions_degrades_on_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_sessions(run_id="r1")
    finally:
        _clear_transport()
    assert out == []


# ── fetch_tcts / fetch_delegations (observability projections) ───────────────


@pytest.mark.asyncio
async def test_fetch_tcts_disabled_returns_empty() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.fetch_tcts(issuer="aid:x") == []


@pytest.mark.asyncio
async def test_fetch_tcts_passes_filters_and_unwraps() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"tcts": [{"jti": "t1"}, {"jti": "t2"}]})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_tcts(capability="write.content", active=True, limit=10)
    finally:
        _clear_transport()
    assert "capability=write.content" in captured["url"]
    assert "active=true" in captured["url"]  # bool mapped to "true", not "True"
    assert "/api/tcts" in captured["url"]
    assert [t["jti"] for t in out] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_fetch_tcts_omits_none_filters() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"tcts": []})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        await cp.fetch_tcts(issuer="aid:i")
    finally:
        _clear_transport()
    # active is None → must not appear; only issuer + limit do.
    assert "active=" not in captured["url"]
    assert "subject=" not in captured["url"]
    assert "issuer=aid%3Ai" in captured["url"]


@pytest.mark.asyncio
async def test_fetch_delegations_walks_root_jti() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"delegations": [{"jti": "d1", "parent_jti": None}]})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_delegations(root_jti="root-1")
    finally:
        _clear_transport()
    assert "root_jti=root-1" in captured["url"]
    assert out[0]["jti"] == "d1"


@pytest.mark.asyncio
async def test_replay_session_empty_session_id_returns_empty() -> None:
    cp = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert await cp.replay_session("") == []


@pytest.mark.asyncio
async def test_replay_session_hits_replay_path() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"events": [{"type": "handshake.started"}]})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.replay_session("s-9", limit=50)
    finally:
        _clear_transport()
    assert "/api/sessions/s-9/replay" in captured["url"]
    assert out[0]["type"] == "handshake.started"


# ── dashboards ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_overview_disabled_returns_empty_dict() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.fetch_dashboard_overview() == {}


@pytest.mark.asyncio
async def test_dashboard_overview_passes_window_and_returns_dict() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"agents": 3, "handshakes": 12})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_dashboard_overview(window="7d")
    finally:
        _clear_transport()
    assert "window=7d" in captured["url"]
    assert out["handshakes"] == 12


@pytest.mark.asyncio
async def test_dashboard_overview_degrades_to_empty_dict() -> None:
    _set_transport(httpx.MockTransport(lambda r: httpx.Response(500)))
    try:
        cp = _client(httpx.MockTransport(lambda r: httpx.Response(500)))
        assert await cp.fetch_dashboard_overview() == {}
    finally:
        _clear_transport()


# ── trust-anchor / pinned-key upsert (write) ─────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_trust_anchor_disabled_returns_none() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.upsert_trust_anchor(issuer_url="https://issuer") is None


@pytest.mark.asyncio
async def test_upsert_trust_anchor_posts_camelcase_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"id": "ta-1", "issuerUrl": "https://issuer"})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler), api_key="k")
        out = await cp.upsert_trust_anchor(
            issuer_url="https://issuer", namespace="demo", label="demo-oidc",
        )
    finally:
        _clear_transport()
    assert out is not None and out["id"] == "ta-1"
    assert captured["url"].endswith("/api/trust-anchors")
    assert '"issuerUrl":"https://issuer"' in captured["body"]
    assert '"namespace":"demo"' in captured["body"]


@pytest.mark.asyncio
async def test_upsert_pinned_key_requires_aid_and_pubkey() -> None:
    cp = _client(httpx.MockTransport(lambda r: httpx.Response(201, json={})))
    assert await cp.upsert_pinned_key(aid="", pubkey="abc") is None
    assert await cp.upsert_pinned_key(aid="aid:x", pubkey="") is None


@pytest.mark.asyncio
async def test_upsert_pinned_key_posts_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"aid": "aid:x"})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.upsert_pinned_key(aid="aid:x", pubkey="pk43", namespace="demo")
    finally:
        _clear_transport()
    assert out is not None
    assert '"aid":"aid:x"' in captured["body"]
    assert '"pubkey":"pk43"' in captured["body"]
