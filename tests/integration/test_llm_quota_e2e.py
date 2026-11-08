"""LLM quota, end to end: real agent subprocesses ask a real playground
server (listening on ``PLAYGROUND_INTERNAL_URL``) before calling the
provider.

* Zero quota: the writer is refused, runs its stub, and the run succeeds
  with ``llm_quota_exceeded``, one ``llm.quota_exceeded`` event and
  ``llm_calls_on_mock > 0``. The provider is a closed port, so a real call
  would have failed the run: success proves nothing reached it.
* Positive control: a generous quota lets the writer through to the closed
  port and the run fails. Without this, the first case could pass because
  the real path never ran at all.

Needs the ``researcher`` and ``writer`` extras (CrewAI, LangChain,
langchain-openai) so both agents' real paths exist. Gated on ``AITP_E2E=1``:

    AITP_E2E=1 uv run --extra researcher --extra writer pytest tests/integration/test_llm_quota_e2e.py -v
"""
from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AITP_E2E"),
    reason="Live subprocess test — set AITP_E2E=1 to enable",
)

aitp = pytest.importorskip("aitp")
pytest.importorskip("langchain_openai", reason="needs the writer extra (uv run --extra writer)")
pytest.importorskip("langchain_core.messages", reason="needs the writer extra")
pytest.importorskip("crewai", reason="needs the researcher extra (uv run --extra researcher)")

import uvicorn  # noqa: E402

from aitp_playground.config import Settings  # noqa: E402
from aitp_playground.main import create_app  # noqa: E402
from tests._auth import AUTH  # noqa: E402

_TERMINAL = {"success", "failed", "cancelled"}
#: Call units the research-and-write scenario asks for: researcher
#: (``MAX_ITER + 1``) plus writer (1). Kept in step with the agents.
RESEARCH_CALLS = 4
WRITE_CALLS = 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def provider_env(monkeypatch) -> None:
    """A fake OpenAI key pointed at a port nothing listens on. Agent
    subprocesses inherit the environment."""
    closed = f"http://127.0.0.1:{_free_port()}/v1"
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-000000000000")
    monkeypatch.setenv("OPENAI_BASE_URL", closed)
    monkeypatch.setenv("OPENAI_API_BASE", closed)
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CP_BASE_URL", "")


def _serve(**quota) -> Iterator[str]:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        playground_internal_url=base, llm_quota_store="memory", cp_base_url="", **quota,
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
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def zero_quota(provider_env) -> Iterator[str]:
    yield from _serve(llm_quota_global_per_day=0)


@pytest.fixture
def open_quota(provider_env) -> Iterator[str]:
    yield from _serve()


def _charged(base: str) -> float:
    """``aitp_playground_llm_quota_charged_total`` from ``/metrics``."""
    text = httpx.get(f"{base}/metrics", headers=AUTH, timeout=10, trust_env=False).text
    for line in text.splitlines():
        if line.startswith("aitp_playground_llm_quota_charged_total"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _run(base: str) -> dict:
    with httpx.Client(base_url=base, headers=AUTH, timeout=10, trust_env=False) as c:
        posted = c.post("/runs", json={
            "scenario_ref": "intra-org/research-and-write@1.0.0",
            "inputs": {"topic": "AITP quota self-test"},
        })
        assert posted.status_code == 202, posted.text
        rid = posted.json()["run_id"]
        deadline = time.time() + 120
        body: dict = {}
        while time.time() < deadline:
            body = c.get(f"/runs/{rid}").json()
            if body.get("status") in _TERMINAL:
                body["_status"] = c.get(f"/runs/{rid}/status").json()
                return body
            time.sleep(0.5)
    pytest.fail(f"run {rid} did not finish in 120s; last status={body.get('status')}")


def test_zero_quota_runs_on_the_mock_and_flags_the_run(zero_quota) -> None:
    body = _run(zero_quota)
    assert body["status"] == "success", body.get("error")
    assert body["llm_quota_exceeded"] is True
    assert body["llm_quota"]["reason"] == "quota_exceeded"
    assert body["llm_quota"]["quota"] == "global_daily"
    assert body["llm_quota"]["limit"] == 0
    assert body["llm_quota"]["resets_at"]
    # Both agents were refused: the researcher's units and the writer's.
    assert body["llm_calls_on_mock"] == RESEARCH_CALLS + WRITE_CALLS
    assert body["_status"]["llm_quota_exceeded"] is True

    quota_events = [e for e in body["events"] if e["type"] == "llm.quota_exceeded"]
    assert len(quota_events) == 1, quota_events
    assert quota_events[0]["capability"]
    types = [e["type"] for e in body["events"]]
    assert "llm.failed" not in types
    mocked = [e for e in body["events"]
              if e["type"] in ("llm.started", "llm.complete") and e.get("notes")]
    assert mocked, "llm.started/llm.complete should carry the mock note"
    assert "write" in body["outputs"]
    assert {e.get("agent_id") for e in mocked} >= {"researcher", "writer"}


def test_open_quota_reaches_the_provider(open_quota) -> None:
    """Positive control: with quota left the writer really dials the
    provider (a closed port here), so the run fails and no quota flag is
    set."""
    before = _charged(open_quota)
    body = _run(open_quota)
    assert body["status"] == "failed"
    # The quota really was charged before the provider was dialled.
    assert _charged(open_quota) > before
    assert body["llm_quota_exceeded"] is False
    assert body["llm_calls_on_mock"] == 0
    types = [e["type"] for e in body["events"]]
    assert "llm.failed" in types, sorted(set(types))
    assert "llm.quota_exceeded" not in types
