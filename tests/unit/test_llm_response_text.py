"""Agents must turn every chat-model response shape into plain text.

LangChain's Anthropic client returns ``AIMessage.content`` as a list of
content blocks, not a string. Before the fix the writer returned that list
as its ``article`` and the analyzer stored it as ``summary``/``risks`` —
interpolating the list's Python repr (thinking/tool_use blocks included)
into its critique prompt. Same bug as acdp-playground #131.

Nothing here may skip: ``tests/integration/test_stable_only_sdk.py``
requires that the only skips in ``tests/unit`` are SDK-feature tests. The
writer/analyzer cases use the real ``langchain_core``/``langgraph`` when the
agent extras are installed (CI's ``--all-extras`` job), and otherwise a minimal
stand-in registered in ``sys.modules`` for the duration of the test.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import llm

AGENTS = Path(__file__).resolve().parents[2] / "agents"

ANTHROPIC_BLOCKS = [
    {"type": "thinking", "thinking": "internal", "signature": "sig"},
    {"type": "text", "text": "Key risk: margin. "},
    {"type": "tool_use", "id": "t1", "name": "lookup", "input": {}},
    {"type": "text", "text": "Recommendation: hedge."},
]
EXPECTED = "Key risk: margin. Recommendation: hedge."


class _Msg:
    def __init__(self, content: Any):
        self.content = content


class _FakeChatModel:
    """Returns the same message for every call and records the prompts."""

    def __init__(self, message: Any):
        self.message = message
        self.prompts: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.prompts.append(messages)
        return self.message


def _real(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


class _FakeMessage:
    def __init__(self, content: Any):
        self.content = content


class _FakeStateGraph:
    """The slice of ``langgraph.graph.StateGraph`` that graph.py uses."""

    def __init__(self, _schema: Any):
        self.nodes: dict[str, Any] = {}
        self.edges: dict[str, str] = {}
        self.entry = ""

    def add_node(self, name: str, fn: Any) -> None:
        self.nodes[name] = fn

    def set_entry_point(self, name: str) -> None:
        self.entry = name

    def add_edge(self, src: str, dst: str) -> None:
        self.edges[src] = dst

    def compile(self) -> "_FakeStateGraph":
        return self

    async def ainvoke(self, state: dict) -> dict:
        node = self.entry
        while node != "__end__":
            state = await self.nodes[node](state)
            node = self.edges[node]
        return state


@pytest.fixture
def agent_libs(monkeypatch):
    """Real langchain_core/langgraph when installed, else minimal stand-ins."""
    if not _real("langchain_core.messages"):
        msgs = types.ModuleType("langchain_core.messages")
        msgs.HumanMessage = msgs.SystemMessage = _FakeMessage
        core = types.ModuleType("langchain_core")
        core.messages = msgs
        monkeypatch.setitem(sys.modules, "langchain_core", core)
        monkeypatch.setitem(sys.modules, "langchain_core.messages", msgs)
    if not _real("langgraph.graph"):
        graph_mod = types.ModuleType("langgraph.graph")
        graph_mod.StateGraph = _FakeStateGraph
        graph_mod.END = "__end__"
        lg = types.ModuleType("langgraph")
        lg.graph = graph_mod
        monkeypatch.setitem(sys.modules, "langgraph", lg)
        monkeypatch.setitem(sys.modules, "langgraph.graph", graph_mod)


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, AGENTS / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("resp", "expected"),
    [
        (_Msg("plain openai text"), "plain openai text"),
        (_Msg(ANTHROPIC_BLOCKS), EXPECTED),
        (_Msg(["a", {"type": "text", "text": "b"}]), "ab"),
        (_Msg([]), ""),
        ("bare string", "bare string"),
    ],
)
def test_response_text_normalises_every_shape(resp: Any, expected: str) -> None:
    assert llm.response_text(resp) == expected


def test_response_text_matches_langchain_text_property() -> None:
    """Parity with ``AIMessage.text`` where langchain_core is installed."""
    if not _real("langchain_core.messages"):
        assert llm.response_text(_Msg(ANTHROPIC_BLOCKS)) == EXPECTED
        return
    from langchain_core.messages import AIMessage  # type: ignore

    msg = AIMessage(content=ANTHROPIC_BLOCKS)
    assert llm.response_text(msg) == msg.text == EXPECTED


def test_writer_returns_text_for_anthropic_blocks(monkeypatch, agent_libs) -> None:
    chain = _load("writer/chain.py", "_writer_chain_under_test")
    fake = _FakeChatModel(_Msg(ANTHROPIC_BLOCKS))
    monkeypatch.setattr(chain, "build_chat_model", lambda: fake)

    out = asyncio.run(chain.run_writer("findings", style="casual"))

    assert out == EXPECTED
    assert len(fake.prompts) == 1


def test_analyzer_returns_text_and_prompts_with_text(monkeypatch, agent_libs) -> None:
    graph = _load("analyzer/graph.py", "_analyzer_graph_under_test")
    fake = _FakeChatModel(_Msg(ANTHROPIC_BLOCKS))
    monkeypatch.setattr(graph, "build_chat_model", lambda: fake)

    out = asyncio.run(graph.run_analyzer("some input"))

    assert out == {"summary": EXPECTED, "risks": EXPECTED}
    # The critique prompt carries the summary's text, never the block list.
    critique_prompt = fake.prompts[1][0].content
    assert critique_prompt.endswith(EXPECTED)
    assert "tool_use" not in critique_prompt and "thinking" not in critique_prompt
