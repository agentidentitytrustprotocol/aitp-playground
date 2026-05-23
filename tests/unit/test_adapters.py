"""Adapter validate + prepare_launch tests — no subprocess actually spawned."""
from __future__ import annotations

from aitp_playground.config import Settings
from aitp_playground.hosting.adapters.registry import build_default_adapter_registry
from aitp_playground.registry.service import RegistryService


def test_crewai_adapter_prepares_launch() -> None:
    settings = Settings()
    svc = RegistryService(settings)
    m = svc.get_agent_manifest("_shared/agents/researcher")
    adapters = build_default_adapter_registry()
    adapter = adapters.get(m.metadata.framework)
    validation = adapter.validate(m)
    assert validation.valid, validation.errors

    launch = adapter.prepare_launch(m, "/tmp/bootstrap.json", 8100, settings)
    assert launch.command.endswith("python3") or launch.command.endswith("python")
    assert launch.args == ["-m", "researcher.main"]
    assert launch.env["AITP_BOOTSTRAP_FILE"] == "/tmp/bootstrap.json"
    assert launch.env["AGENT_PORT"] == "8100"
    # PYTHONPATH must include the agents/base directory so `from bootstrap` works
    assert "agents/base" in launch.env["PYTHONPATH"]


def test_langchain_adapter_writer_manifest() -> None:
    settings = Settings()
    svc = RegistryService(settings)
    m = svc.get_agent_manifest("_shared/agents/writer")
    adapter = build_default_adapter_registry().get(m.metadata.framework)
    assert adapter.validate(m).valid
    launch = adapter.prepare_launch(m, "/tmp/bs.json", 8101, settings)
    assert launch.args == ["-m", "writer.main"]


def test_langgraph_adapter_analyzer_manifest() -> None:
    settings = Settings()
    svc = RegistryService(settings)
    m = svc.get_agent_manifest("_shared/agents/analyzer")
    adapter = build_default_adapter_registry().get(m.metadata.framework)
    assert adapter.validate(m).valid
    launch = adapter.prepare_launch(m, "/tmp/bs.json", 8102, settings)
    assert launch.args == ["-m", "analyzer.main"]
