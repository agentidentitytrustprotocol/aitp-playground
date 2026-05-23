"""BootstrapBuilder produces valid bootstrap JSON."""
from __future__ import annotations

import json
from pathlib import Path

from aitp_playground.config import Settings
from aitp_playground.hosting.bootstrap import BootstrapBuilder
from aitp_playground.hosting.identity import derive_seed_hex
from aitp_playground.registry.models import AgentSpec
from aitp_playground.registry.service import RegistryService


def test_build_and_write_produces_64_char_seed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate tempdir-ish; harmless
    settings = Settings()
    svc = RegistryService(settings)
    manifest = svc.get_agent_manifest("_shared/agents/researcher")
    builder = BootstrapBuilder(settings)

    agent_spec = AgentSpec(id="researcher", ref="_shared/agents/researcher")
    bs = builder.build(
        run_id="run-abc",
        agent_spec=agent_spec,
        resolved_manifest=manifest,
        port=8100,
        peers={"writer": {"manifest_url": "http://localhost:8101/.well-known/aitp-manifest", "did": None}},
        inputs={"topic": "AITP"},
    )
    assert len(bs["aitp"]["seed_hex"]) == 64
    assert bs["aitp"]["handshake_endpoint"] == "http://localhost:8100/aitp/handshake/hello"
    assert bs["aitp"]["offered_caps"] == ["research.query"]
    assert bs["peers"]["writer"]["manifest_url"].endswith("/.well-known/aitp-manifest")

    path = builder.write(bs)
    assert Path(path).exists()
    reloaded = json.loads(Path(path).read_text())
    assert reloaded == bs


def test_derive_seed_hex_namespacing_changes_seed() -> None:
    internal = derive_seed_hex("run-1", "analyzer", org="internal")
    external = derive_seed_hex("run-1", "analyzer", org="external")
    assert internal != external
    assert len(internal) == len(external) == 64
