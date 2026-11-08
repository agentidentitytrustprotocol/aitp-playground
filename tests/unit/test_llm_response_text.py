"""Agents must turn every chat-model response shape into plain text.

LangChain's Anthropic client returns ``AIMessage.content`` as a list of
content blocks, not a string. Before the fix the writer returned that list
as its ``article`` and the analyzer stored it as ``summary``/``risks`` —
interpolating the list's Python repr (thinking/tool_use blocks included)
into its critique prompt. Same bug as acdp-playground #131.

The shape matrix runs everywhere. The writer/analyzer cases need
``langchain_core``/``langgraph`` (the agent extras) and skip without them;
CI's ``--all-extras`` job runs them.
"""
from __future__ import annotations

import asyncio
import importlib.util
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
    messages = pytest.importorskip("langchain_core.messages")
    msg = messages.AIMessage(content=ANTHROPIC_BLOCKS)
    assert llm.response_text(msg) == msg.text == EXPECTED


def test_writer_returns_text_for_anthropic_blocks(monkeypatch) -> None:
    pytest.importorskip("langchain_core.messages")
    chain = _load("writer/chain.py", "_writer_chain_under_test")
    fake = _FakeChatModel(_Msg(ANTHROPIC_BLOCKS))
    monkeypatch.setattr(chain, "build_chat_model", lambda: fake)

    out = asyncio.run(chain.run_writer("findings", style="casual"))

    assert out == EXPECTED
    assert len(fake.prompts) == 1


def test_analyzer_returns_text_and_prompts_with_text(monkeypatch) -> None:
    pytest.importorskip("langchain_core.messages")
    pytest.importorskip("langgraph")
    graph = _load("analyzer/graph.py", "_analyzer_graph_under_test")
    fake = _FakeChatModel(_Msg(ANTHROPIC_BLOCKS))
    monkeypatch.setattr(graph, "build_chat_model", lambda: fake)

    out = asyncio.run(graph.run_analyzer("some input"))

    assert out == {"summary": EXPECTED, "risks": EXPECTED}
    # The critique prompt carries the summary's text, never the block list.
    critique_prompt = fake.prompts[1][0].content
    assert critique_prompt.endswith(EXPECTED)
    assert "tool_use" not in critique_prompt and "thinking" not in critique_prompt
