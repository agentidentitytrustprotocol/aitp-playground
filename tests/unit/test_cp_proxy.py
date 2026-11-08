"""GET /cp-proxy/{path}: key-gated, allowlisted, GET-only passthrough to the CP."""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from aitp_playground.api.cp_proxy import (
    ALLOWED,
    MAX_BODY_BYTES,
    MAX_STREAMS,
    CpProxy,
    resolve_target,
)
from aitp_playground.config import Settings
from aitp_playground.main import create_app
from tests._auth import AUTH, TEST_API_KEY

CP = "http://cp.test"


class FakeCp:
    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.handler = handler or (lambda r: httpx.Response(200, json={"ok": True, "path": r.url.path}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _app(fake: FakeCp | None = None, **settings):
    s = Settings(**{"cp_base_url": CP, "cp_api_key": "cp-admin-key", **settings})
    app = create_app(s)
    app.state.cp_proxy = CpProxy(s, transport=httpx.MockTransport(fake or FakeCp()))
    return app


def _client(app, headers=AUTH) -> TestClient:
    return TestClient(app, headers=headers)


# --- allowlist ----------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "api/health", "api/readyz", "api/dashboard/overview", "api/dashboard/agents",
    "api/delegations", "api/events/history", "api/pinned-keys", "api/registry/agents",
    "api/registry/agents/aid:abc", "api/registry/agents/aid:abc/manifest", "api/sessions",
    "api/sessions/s-1", "api/sessions/s-1/replay", "api/tcts", "api/trust-anchors",
    "api/trust-anchors/ta-1", ".well-known/aitp-manifest",
])
def test_allowlisted_get_reaches_the_cp_with_the_service_key(path):
    fake = FakeCp()
    r = _client(_app(fake)).get(f"/cp-proxy/{path}?limit=5&run_id=r1",
                                headers={"accept": "application/json", "if-none-match": "W/1",
                                         "x-forwarded-for": "203.0.113.9", "cookie": "a=b"})
    assert r.status_code == 200, r.text
    (req,) = fake.requests
    assert str(req.url).startswith(f"{CP}/{path.replace(':', ':')}")
    assert req.url.query == b"limit=5&run_id=r1"
    assert req.method == "GET"
    assert req.headers["authorization"] == "Bearer cp-admin-key"
    assert req.headers["accept"] == "application/json"
    assert req.headers["if-none-match"] == "W/1"
    for h in ("x-api-key", "cookie", "x-forwarded-for"):
        assert h not in req.headers
    assert TEST_API_KEY not in str(req.headers)


def test_every_allowlist_entry_is_covered_above():
    assert len(ALLOWED) == 18


def test_read_key_is_preferred_over_the_admin_key():
    fake = FakeCp()
    _client(_app(fake, cp_read_api_key="cp-read-key")).get("/cp-proxy/api/tcts")
    assert fake.requests[0].headers["authorization"] == "Bearer cp-read-key"


def test_caller_bearer_is_never_forwarded():
    fake = FakeCp()
    app = _app(fake, cp_api_key="")
    r = TestClient(app).get("/cp-proxy/api/tcts",
                            headers={"Authorization": f"Bearer {TEST_API_KEY}"})
    assert r.status_code == 200
    assert "authorization" not in fake.requests[0].headers


@pytest.mark.parametrize("path", [
    "api/registry/agents/aid1/export", "api/trust-anchors/t1/jwks", "api/audit",
    "api/sessions/s1/export", "api/webhooks", "api/webhooks/w1", "api/revocations",
    "api/revocation/issuers", "api/metrics", "api/events", "api", "", "api/health/",
    "api//health", "API/health", "api/registry/agents/a/manifest/x",
    "api/sessions/%2e%2e", "api/sessions/%2E%2E/replay",
    "api/sessions/%2e", "api/registry/agents/a%2fb",
    "api/registry/agents/a%2Fb/manifest", "api/sessions/a%5cb", "api/sessions/%00",
    "api/sessions/%252e%252e", "api/sessions/%ff", "api/health%2f..%2fadmin",
])
def test_unknown_or_traversal_is_404_with_no_upstream_call(path):
    fake = FakeCp()
    r = _client(_app(fake)).get(f"/cp-proxy/{path}")
    assert r.status_code == 404, (path, r.status_code)
    assert r.json()["code"] == "not_found"
    assert fake.requests == []


async def _raw_get(app, raw_path: bytes) -> int:
    """Send a GET with exactly this request-target. httpx normalises dot
    segments even in a hand-set raw path, so the ASGI scope is built here."""
    path = raw_path.decode("latin-1")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": raw_path, "root_path": "",
        "query_string": b"", "headers": [(b"host", b"t"), (b"authorization", AUTH["Authorization"].encode())],
        "client": ("203.0.113.1", 1234), "server": ("t", 80),
    }
    status: list[int] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])

    await app(scope, receive, send)
    return status[0]


@pytest.mark.parametrize("raw", [
    b"/cp-proxy/api/sessions/../../api/audit", b"/cp-proxy/api/sessions/..",
    b"/cp-proxy/api/sessions/.", b"/cp-proxy/api/sessions/./replay",
    b"/cp-proxy/./api/health", b"/cp-proxy/api/sessions/x/../../audit",
    b"/cp-proxy/api/sessions/%2e", b"/cp-proxy/api/registry/agents/a%2F..%2F..%2Faudit",
])
def test_raw_dot_segments_cannot_escape(raw):
    fake = FakeCp()
    assert asyncio.run(_raw_get(_app(fake), raw)) == 404, raw
    assert fake.requests == []


def test_raw_get_helper_reaches_an_allowed_route():
    fake = FakeCp()
    assert asyncio.run(_raw_get(_app(fake), b"/cp-proxy/api/sessions/s1/replay")) == 200
    assert fake.requests[0].url.raw_path == b"/api/sessions/s1/replay"


def test_segments_are_re_encoded_upstream():
    fake = FakeCp()
    r = _client(_app(fake)).get("/cp-proxy/api/registry/agents/aid%3Aw%20x%3Fy")
    assert r.status_code == 200
    assert fake.requests[0].url.raw_path == b"/api/registry/agents/aid:w%20x%3Fy"


def test_resolve_target_marks_only_the_event_stream_as_streamed():
    assert resolve_target("api/events/stream") == ("api/events/stream", True)
    assert resolve_target("api/events/history") == ("api/events/history", False)
    assert resolve_target("api/events/../audit") is None


# --- auth ---------------------------------------------------------------------

@pytest.mark.parametrize("require", [False, True])
def test_keyless_is_401_whatever_the_flag(require, monkeypatch):
    fake = FakeCp()
    app = _app(fake, aitp_playground_require_api_key=require)
    r = TestClient(app).get("/cp-proxy/api/health")
    assert r.status_code == 401
    r = TestClient(app).get("/cp-proxy/api/health", headers={"x-api-key": "wrong"})
    assert r.status_code == 401
    assert fake.requests == []


def test_x_api_key_header_is_accepted():
    fake = FakeCp()
    r = TestClient(_app(fake)).get("/cp-proxy/api/health", headers={"x-api-key": TEST_API_KEY})
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_non_get_is_405_and_never_forwarded(method):
    fake = FakeCp()
    r = _client(_app(fake)).request(method, "/cp-proxy/api/tcts")
    assert r.status_code == 405
    assert fake.requests == []


# --- upstream behaviour -------------------------------------------------------

def test_cp_unset_is_503():
    app = create_app(Settings(cp_base_url=""))
    r = _client(app).get("/cp-proxy/api/health")
    assert r.status_code == 503 and r.json()["code"] == "cp_disabled"


def test_cp_unset_still_404s_unknown_paths_first():
    app = create_app(Settings(cp_base_url=""))
    assert _client(app).get("/cp-proxy/api/audit").status_code == 404


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout])
def test_upstream_transport_error_is_502(exc):
    def boom(request):
        raise exc("down", request=request)

    r = _client(_app(FakeCp(boom))).get("/cp-proxy/api/health")
    assert r.status_code == 502 and r.json()["code"] == "cp_unreachable"


@pytest.mark.parametrize("status", [304, 401, 404, 500, 503])
def test_upstream_status_and_allowlisted_headers_pass_through(status):
    def handler(request):
        body = b"" if status == 304 else b'{"error":"x"}'
        return httpx.Response(status, content=body, headers={
            "content-type": "application/json", "etag": "W/2", "cache-control": "no-store",
            "set-cookie": "s=1", "x-powered-by": "next", "www-authenticate": "Bearer",
            "location": "http://evil.test/",
        })

    r = _client(_app(FakeCp(handler))).get("/cp-proxy/api/tcts")
    assert r.status_code == status
    assert r.headers.get("etag") == "W/2"
    assert r.headers.get("cache-control") == "no-store"
    for h in ("set-cookie", "x-powered-by", "www-authenticate", "location"):
        assert h not in r.headers


def test_redirect_is_not_followed():
    fake = FakeCp(lambda r: httpx.Response(302, headers={"location": f"{CP}/api/audit"}))
    r = _client(_app(fake)).get("/cp-proxy/api/tcts", follow_redirects=False)
    assert r.status_code == 302
    assert "location" not in r.headers
    assert len(fake.requests) == 1


def test_body_over_the_cap_is_502():
    big = b"x" * (MAX_BODY_BYTES + 1)

    def chunked(request):
        async def stream():
            for i in range(0, len(big), 1 << 20):
                yield big[i:i + (1 << 20)]
        return httpx.Response(200, content=stream())

    r = _client(_app(FakeCp(chunked))).get("/cp-proxy/api/sessions")
    assert r.status_code == 502 and r.json()["code"] == "cp_response_too_large"


def test_declared_length_over_the_cap_is_502_without_reading():
    fake = FakeCp(lambda r: httpx.Response(
        200, content=b"{}", headers={"content-length": str(MAX_BODY_BYTES + 1)}))
    r = _client(_app(fake)).get("/cp-proxy/api/sessions")
    assert r.status_code == 502 and r.json()["code"] == "cp_response_too_large"


def test_body_at_the_cap_passes():
    body = b"y" * MAX_BODY_BYTES
    r = _client(_app(FakeCp(lambda r: httpx.Response(200, content=body)))).get(
        "/cp-proxy/api/sessions")
    assert r.status_code == 200 and len(r.content) == MAX_BODY_BYTES


# --- event stream -------------------------------------------------------------

def test_event_stream_chunks_arrive_incrementally_and_disconnect_closes_upstream():
    """Over a real server (httpx's ASGITransport buffers whole responses):
    each event reaches the caller while the upstream is still open, and
    closing the caller's connection closes the upstream stream."""
    import threading
    import time

    import uvicorn

    closed = threading.Event()
    sent: list[int] = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(2):
                sent.append(i)
                yield f"data: {i}\n\n".encode()
            await asyncio.sleep(30)  # an idle, open stream
            yield b"data: never\n\n"

        async def aclose(self):
            closed.set()

    fake = FakeCp(lambda r: httpx.Response(
        200, stream=Stream(), headers={"content-type": "text/event-stream"}))
    app = _app(fake)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started:
            assert time.time() < deadline and thread.is_alive(), "server did not start"
            time.sleep(0.02)
        t0 = time.monotonic()
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as c:
            with c.stream("GET", "/cp-proxy/api/events/stream",
                          headers={**AUTH, "last-event-id": "7"}, timeout=10) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                assert r.headers["x-accel-buffering"] == "no"
                got = b""
                for chunk in r.iter_raw():
                    got += chunk
                    if got.count(b"\n\n") >= 2:
                        break
        # Both events arrived long before the upstream's 30 s idle ended.
        assert time.monotonic() - t0 < 10
        assert got == b"data: 0\n\ndata: 1\n\n"
        assert closed.wait(timeout=10), "upstream stream was not closed on disconnect"
        assert fake.requests[0].headers["last-event-id"] == "7"
        deadline = time.time() + 10
        while app.state.cp_proxy.open_streams and time.time() < deadline:
            time.sleep(0.02)
        assert app.state.cp_proxy.open_streams == 0, "stream slot not freed on disconnect"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_event_stream_error_status_is_buffered_through():
    fake = FakeCp(lambda r: httpx.Response(401, json={"error": "unauthorized"}))
    r = _client(_app(fake)).get("/cp-proxy/api/events/stream")
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


def test_proxy_client_is_shared_and_closed():
    s = Settings(cp_base_url=CP)
    proxy = CpProxy(s)
    assert proxy.client() is proxy.client()
    asyncio.run(proxy.aclose())
    assert proxy._client is None


# --- stream cap and deadlines -------------------------------------------------

def test_stream_cap_is_503_with_no_upstream_call():
    fake = FakeCp()
    app = _app(fake)
    app.state.cp_proxy.open_streams = MAX_STREAMS
    c = _client(app)
    r = c.get("/cp-proxy/api/events/stream")
    assert r.status_code == 503 and r.json()["code"] == "too_many_streams"
    assert fake.requests == []
    assert app.state.cp_proxy.open_streams == MAX_STREAMS
    # Buffered reads are not affected by the stream cap.
    assert c.get("/cp-proxy/api/tcts").status_code == 200


def test_stream_slot_is_freed_after_an_error_status():
    app = _app(FakeCp(lambda r: httpx.Response(503, json={"error": "down"})))
    r = _client(app).get("/cp-proxy/api/events/stream")
    assert r.status_code == 503
    assert app.state.cp_proxy.open_streams == 0


def test_stream_slot_is_freed_after_a_transport_error():
    def boom(r):
        raise httpx.ConnectError("refused", request=r)

    app = _app(FakeCp(boom))
    r = _client(app).get("/cp-proxy/api/events/stream")
    assert r.status_code == 502 and r.json()["code"] == "cp_unreachable"
    assert app.state.cp_proxy.open_streams == 0


def test_stream_slot_is_freed_after_a_finished_stream():
    app = _app(FakeCp(lambda r: httpx.Response(
        200, content=b"data: 1\n\n", headers={"content-type": "text/event-stream"})))
    r = _client(app).get("/cp-proxy/api/events/stream")
    assert r.status_code == 200 and r.content == b"data: 1\n\n"
    assert app.state.cp_proxy.open_streams == 0


class _Drip(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(100):
            yield b"x"
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("path,status", [("api/tcts", 200), ("api/events/stream", 500)])
def test_slow_body_hits_the_total_deadline(path, status):
    """A body that drips bytes (each read inside the per-read timeout) is cut
    off by the whole-body deadline; a non-200 event-stream reply too."""
    app = _app(FakeCp(lambda r: httpx.Response(status, stream=_Drip())),
               cp_proxy_timeout_seconds=0.3)
    r = _client(app).get(f"/cp-proxy/{path}")
    assert r.status_code == 502 and r.json()["code"] == "cp_unreachable"
    assert app.state.cp_proxy.open_streams == 0
