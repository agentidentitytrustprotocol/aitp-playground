"""Trust resolver helpers — pure encoding + did:web → manifest-URL resolution.

The network path (``resolve_did_web``) is exercised with httpx.MockTransport so
no real DID host is needed.
"""
from __future__ import annotations

import httpx
import pytest

from aitp_playground.trust import resolver
from aitp_playground.trust.resolver import encode_did_web, resolve_did_web


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost:8101", "did:web:localhost%3A8101"),
        ("example.com", "did:web:example.com"),
        ("sub.example.com:443", "did:web:sub.example.com%3A443"),
        ("a.b.c", "did:web:a.b.c"),  # dots stay unescaped (safe='.')
        ("", "did:web:"),
    ],
)
def test_encode_did_web(host: str, expected: str) -> None:
    assert encode_did_web(host) == expected


def test_encode_did_web_round_trips_through_resolver_unquote() -> None:
    from urllib.parse import unquote

    did = encode_did_web("localhost:8101")
    assert unquote(did[len("did:web:"):]) == "localhost:8101"


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_ctor = httpx.AsyncClient

    def _wrapper(*args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_ctor(*args, **kwargs)

    monkeypatch.setattr(resolver.httpx, "AsyncClient", _wrapper)


async def test_resolve_did_web_rejects_non_did_web() -> None:
    with pytest.raises(ValueError, match="not a did:web"):
        await resolve_did_web("did:key:zABC")


async def test_resolve_did_web_returns_manifest_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "service": [
                    {"type": "Other", "serviceEndpoint": "https://nope"},
                    {"type": "AitpManifest", "serviceEndpoint": "https://example.com/"},
                ]
            },
        )

    _patch_transport(monkeypatch, handler)
    url = await resolve_did_web("did:web:example.com")
    # A real host → https; trailing slash trimmed before the suffix is appended.
    assert captured["url"] == "https://example.com/.well-known/did.json"
    assert url == "https://example.com/.well-known/aitp-manifest"


async def test_resolve_did_web_uses_http_for_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"service": [{"type": "AitpManifest", "serviceEndpoint": "http://localhost:8101"}]},
        )

    _patch_transport(monkeypatch, handler)
    url = await resolve_did_web("did:web:localhost%3A8101")
    # The %3A is unquoted back to ':' and localhost forces http (not https).
    assert captured["url"] == "http://localhost:8101/.well-known/did.json"
    assert url == "http://localhost:8101/.well-known/aitp-manifest"


async def test_resolve_did_web_raises_when_no_aitp_service(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"service": [{"type": "Other", "serviceEndpoint": "x"}]})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(ValueError, match="no AitpManifest service"):
        await resolve_did_web("did:web:example.com")


async def test_resolve_did_web_propagates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await resolve_did_web("did:web:example.com")
