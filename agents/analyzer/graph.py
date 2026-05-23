"""LangGraph analyzer graph — with a deterministic fallback when LangGraph isn't installed."""
from __future__ import annotations

import os
from typing import Any


def _has_langgraph_and_key() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import langgraph  # noqa: F401
        import langchain_anthropic  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def run_analyzer(input_text: str) -> dict[str, Any]:
    """Run an analysis pass over the input. Returns a structured summary."""
    if _has_langgraph_and_key():
        from langchain_anthropic import ChatAnthropic  # type: ignore
        from langgraph.graph import END, StateGraph  # type: ignore
        from typing_extensions import TypedDict  # noqa: F401

        class State(dict):  # simple state container
            pass

        llm = ChatAnthropic(model="claude-sonnet-4-6")

        def summarise(state: dict) -> dict:
            from langchain_core.messages import HumanMessage  # type: ignore
            resp = llm.invoke([HumanMessage(content=f"Summarise in 3 bullets:\n\n{state['text']}")])
            return {"text": state["text"], "summary": resp.content}

        def critique(state: dict) -> dict:
            from langchain_core.messages import HumanMessage  # type: ignore
            resp = llm.invoke([HumanMessage(content=f"Give two concrete risks for:\n\n{state['summary']}")])
            return {**state, "risks": resp.content}

        g = StateGraph(dict)
        g.add_node("summarise", summarise)
        g.add_node("critique", critique)
        g.set_entry_point("summarise")
        g.add_edge("summarise", "critique")
        g.add_edge("critique", END)
        compiled = g.compile()
        result = compiled.invoke({"text": input_text})
        return {
            "summary": result.get("summary"),
            "risks": result.get("risks"),
        }

    return _stub_analyze(input_text)


def _stub_analyze(input_text: str) -> dict[str, Any]:
    bullets = [line.strip("- *• ") for line in input_text.splitlines() if line.strip()][:3]
    summary = "\n".join(f"- {b}" for b in bullets) or "- (empty input)"
    return {
        "summary": summary,
        "risks": (
            "- Misaligned grants between issuer and consumer\n"
            "- TCT replay if session bundles are not pinned to the manifest"
        ),
    }
