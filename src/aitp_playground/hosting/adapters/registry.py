"""Adapter registry — pick an adapter by framework."""
from __future__ import annotations

from typing import Dict

from .base import AgentFramework, AgentHostAdapter
from .python_agent import PythonAgentAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._by_framework: Dict[AgentFramework, AgentHostAdapter] = {}

    def register(self, adapter: AgentHostAdapter) -> None:
        self._by_framework[adapter.framework] = adapter

    def get(self, framework: AgentFramework) -> AgentHostAdapter:
        try:
            return self._by_framework[framework]
        except KeyError as exc:
            raise KeyError(f"no adapter registered for framework: {framework}") from exc


def build_default_adapter_registry() -> AdapterRegistry:
    reg = AdapterRegistry()
    for fw in ("crewai", "langchain", "langgraph"):
        reg.register(PythonAgentAdapter(fw, strict=True))
    reg.register(PythonAgentAdapter("custom", strict=False))
    return reg
