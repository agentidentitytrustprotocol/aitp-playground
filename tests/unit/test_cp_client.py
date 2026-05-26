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


# ── fetch_revocation_list ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_revocation_list_disabled_returns_empty() -> None:
    cp = CpClient(Settings(cp_base_url=""))
    assert await cp.fetch_revocation_list() == []


@pytest.mark.asyncio
async def test_fetch_revocation_list_parses_envelope_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "revocation_list": {
                "entries": [
                    {"jti": "jti-a", "revoked_at": "2026-05-25T00:00:00Z"},
                    {"jti": "jti-b"},
                ],
            },
        })

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_revocation_list()
    finally:
        _clear_transport()
    assert set(out) == {"jti-a", "jti-b"}


@pytest.mark.asyncio
async def test_fetch_revocation_list_parses_flat_shape() -> None:
    """CP's wire shape may evolve; the client should tolerate both
    {"revocation_list": {"entries": [...]}} and {"entries": [...]}."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entries": [{"jti": "jti-flat"}]})

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_revocation_list()
    finally:
        _clear_transport()
    assert out == ["jti-flat"]


@pytest.mark.asyncio
async def test_fetch_revocation_list_degrades_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    _set_transport(httpx.MockTransport(handler))
    try:
        cp = _client(httpx.MockTransport(handler))
        out = await cp.fetch_revocation_list()
    finally:
        _clear_transport()
    assert out == []
