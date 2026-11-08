"""Backend lockdown over a real socket (``AITP_PLAYGROUND_REQUIRE_API_KEY=true``).

The unit tests (``tests/unit/test_backend_lockdown.py``) drive the ASGI app
in process. This one starts uvicorn on a loopback port and talks HTTP to it,
so the middleware, the routers and the server's own header handling are the
real ones. Free: no agent is spawned and no LLM is called (every ``POST /runs``
here carries an invalid body, so the run limits run and the request then
stops at 422). No gate, so it runs anywhere ``uv sync`` has run; CI runs it
next to the ``AITP_E2E`` tests (``.github/workflows/ci.yml``).

``X-AITP-Client-IP`` is observed through the per-IP token bucket
(``RATE_LIMIT_BURST=1``): a trusted subject IP gets its own bucket, so a
second keyed launch from the same subject IP is 429 while a different one is
not; a keyless request carrying the header is 401 and charges nothing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from aitp_playground.config import Settings
from aitp_playground.main import create_app

KEY = "lockdown-socket-test-key-not-a-secret"
KEYED = {"Authorization": f"Bearer {KEY}"}
IP = "X-AITP-Client-IP"
#: An invalid run body: the spend limits run first (a dependency), then 422.
BAD_RUN: dict = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    settings = Settings(
        _env_file=None,
        aitp_playground_require_api_key=True,
        playground_api_key=KEY,
        playground_internal_url=url,
        cp_base_url="",
        llm_quota_store="memory",
        run_history_db="",
        rate_limit_per_minute=1,
        rate_limit_burst=1,
        max_runs_per_hour=100,
        run_rate_limit_per_ip=100,
        max_concurrent_runs=100,
    )
    server = uvicorn.Server(uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_level="warning", lifespan="on",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline or not thread.is_alive():
            pytest.fail("playground server did not start")
        time.sleep(0.05)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _get(base: str, path: str, **kw) -> httpx.Response:
    return httpx.get(f"{base}{path}", timeout=10, trust_env=False, **kw)


def _post_run(base: str, headers: dict) -> httpx.Response:
    return httpx.post(f"{base}/runs", json=BAD_RUN, headers=headers, timeout=10, trust_env=False)


def _assert_401(r: httpx.Response) -> None:
    assert r.status_code == 401, (r.status_code, r.text)
    assert r.headers.get("www-authenticate"), dict(r.headers)
    assert r.json()["code"] == "unauthorized"


def test_healthz_is_open(base: str) -> None:
    assert _get(base, "/healthz").status_code == 200


@pytest.mark.parametrize("path", ["/scenarios", "/runs"])
def test_keyless_reads_are_401_with_www_authenticate(base: str, path: str) -> None:
    _assert_401(_get(base, path))


def test_a_wrong_key_is_401(base: str) -> None:
    _assert_401(_get(base, "/scenarios", headers={"Authorization": "Bearer not-the-key-000000"}))


def test_keyless_run_launch_is_401(base: str) -> None:
    _assert_401(_post_run(base, {}))


@pytest.mark.parametrize("path", ["/scenarios", "/runs"])
def test_keyed_reads_are_200(base: str, path: str) -> None:
    assert _get(base, path, headers=KEYED).status_code == 200
    assert _get(base, path, headers={"x-api-key": KEY}).status_code == 200


def test_client_ip_header_is_honoured_only_with_a_key(base: str) -> None:
    # Keyless with the header: refused at the gate, charges no bucket.
    _assert_401(_post_run(base, {IP: "198.51.100.7"}))
    # Keyed, same subject IP: its bucket is untouched, so the limits pass and
    # the invalid body is what stops it.
    assert _post_run(base, {**KEYED, IP: "198.51.100.7"}).status_code == 422
    # The subject IP's own bucket (burst 1) is now empty.
    second = _post_run(base, {**KEYED, IP: "198.51.100.7"})
    assert second.status_code == 429, second.text
    assert second.headers.get("retry-after")
    # A different subject IP is a different bucket.
    assert _post_run(base, {**KEYED, IP: "198.51.100.8"}).status_code == 422
    # A wrong key with the header is 401 and does not get a bucket either.
    _assert_401(_post_run(base, {"Authorization": "Bearer wrong-key-0000000000", IP: "198.51.100.9"}))
    assert _post_run(base, {**KEYED, IP: "198.51.100.9"}).status_code == 422


def test_startup_is_refused_without_playground_api_key(tmp_path) -> None:
    """The real entrypoint (``uvicorn aitp_playground.main:app``) exits before
    it ever listens. Runs in an empty cwd so no ``.env`` is read."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "PLAYGROUND_API_KEY"}
    env.update(AITP_PLAYGROUND_REQUIRE_API_KEY="true", PLAYGROUND_API_KEY="")
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "aitp_playground.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "requires PLAYGROUND_API_KEY" in proc.stderr, proc.stderr[-2000:]
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0
