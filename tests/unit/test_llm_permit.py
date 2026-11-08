"""agents/base/llm.py ``llm_permit``: the agent side of the LLM quota gate.
It fails closed (and reports ``quota_check_failed``) on anything but a clear
yes/no, and a refused builder runs entirely on its stub."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import llm
from tests.unit.test_llm_response_text import agent_libs  # noqa: F401 - fixture

AGENTS = Path(__file__).resolve().parents[2] / "agents"
URL = "http://127.0.0.1:8000/internal/llm-quota"
TOKEN = "tok-test-123"


def _bs(url: str | None = URL, token: str | None = TOKEN) -> dict:
    return {
        "run_id": "run-1", "agent_id": "writer",
        "playground": {"quota_url": url, "telemetry_token": token},
    }


class _Emit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, type_: str, _bs: dict, **fields: Any) -> None:
        self.events.append((type_, fields))


def _install(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        assert kwargs.get("trust_env") is False
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm.httpx, "AsyncClient", factory)
    return seen


def _permit(bs: dict, emit: _Emit, calls: int = 2) -> bool:
    return asyncio.run(llm.llm_permit(bs, capability="write.content", calls=calls, emit_event=emit))


def test_allowed_sends_run_capability_calls_and_token(monkeypatch):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={"allowed": True}))
    emit = _Emit()
    assert _permit(_bs(), emit) is True
    assert emit.events == []
    (req,) = seen
    assert str(req.url) == URL and req.method == "POST"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.content) == {
        "run_id": "run-1", "capability": "write.content", "calls": 2, "agent_id": "writer",
    }


def test_decided_denial_emits_nothing(monkeypatch):
    # The playground records a decided denial itself.
    _install(monkeypatch, lambda r: httpx.Response(
        200, json={"allowed": False, "reason": "quota_exceeded"}))
    emit = _Emit()
    assert _permit(_bs(), emit) is False
    assert emit.events == []


def _assert_failed_closed(emit: _Emit, calls: int = 2) -> None:
    assert emit.events == [("llm.quota_exceeded", {
        "capability": "write.content", "code": "quota_check_failed", "calls": calls,
    })]


@pytest.mark.parametrize("bs", [_bs(url=None), _bs(token=None), {"run_id": "r"}])
def test_missing_url_or_token_denies_without_a_request(monkeypatch, bs):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={"allowed": True}))
    emit = _Emit()
    assert _permit(bs, emit) is False
    assert seen == []
    _assert_failed_closed(emit)


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"allowed": True}),
    httpx.Response(404, json={"detail": "nope"}),
    httpx.Response(401),
    httpx.Response(200, json={"allowed": "yes"}),
    httpx.Response(200, json=["allowed"]),
    httpx.Response(200, content=b"not json"),
])
def test_bad_answers_deny_and_report(monkeypatch, response):
    _install(monkeypatch, lambda r: response)
    emit = _Emit()
    assert _permit(_bs(), emit) is False
    _assert_failed_closed(emit)


def test_connect_error_denies_and_reports(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    _install(monkeypatch, boom)
    emit = _Emit()
    assert _permit(_bs(), emit) is False
    _assert_failed_closed(emit)


def test_timeout_denies_and_reports(monkeypatch):
    async def hang(*_a, **_k):
        await asyncio.sleep(10)

    monkeypatch.setattr(llm, "_post_quota", hang)
    monkeypatch.setattr(llm, "QUOTA_CHECK_TIMEOUT_S", 0.05)
    emit = _Emit()
    assert _permit(_bs(), emit) is False
    _assert_failed_closed(emit)


def test_calls_are_clamped_to_at_least_one(monkeypatch):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={"allowed": True}))
    assert _permit(_bs(), _Emit(), calls=0) is True
    assert json.loads(seen[0].content)["calls"] == 1


# --- a refused capability runs wholly on its stub ------------------------------

def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, AGENTS / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Exploding:
    async def ainvoke(self, _messages):
        raise AssertionError("a refused capability must not reach the model")


def test_writer_use_llm_false_never_builds_a_model(monkeypatch, agent_libs):  # noqa: F811
    chain = _load("writer/chain.py", "_writer_chain_permit")
    monkeypatch.setattr(chain, "build_chat_model", lambda: _Exploding())
    out = asyncio.run(chain.run_writer("findings", style="casual", use_llm=False))
    assert out == chain._stub_article("findings", "casual")


def test_analyzer_use_llm_false_never_builds_a_model(monkeypatch, agent_libs):  # noqa: F811
    graph = _load("analyzer/graph.py", "_analyzer_graph_permit")
    monkeypatch.setattr(graph, "build_chat_model", lambda: _Exploding())
    out = asyncio.run(graph.run_analyzer("some input", use_llm=False))
    assert out == graph._stub_analyze("some input")


def test_researcher_use_llm_false_returns_the_stub_crew(monkeypatch):
    crew = _load("researcher/crew.py", "_researcher_crew_permit")
    monkeypatch.setattr(crew, "_crewai_ready", lambda: True)
    built = crew.build_crew({"topic": "x"}, use_llm=False)
    assert isinstance(built, crew._StubCrew)
    assert not hasattr(built, "kickoff_async")
