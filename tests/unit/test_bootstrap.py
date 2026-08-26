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


def test_bootstrap_omits_cp_block_when_unconfigured() -> None:
    """Without CP_BASE_URL set the bootstrap must not carry a ``cp`` block —
    the agent's revocation-refresh path keys off its presence.

    We construct Settings via direct kwargs (bypassing the .env file
    pydantic-settings would otherwise load) so the assertion is stable
    in dev environments that have CP_BASE_URL exported.
    """
    settings = Settings(cp_base_url="", cp_api_key="")
    svc = RegistryService(settings)
    manifest = svc.get_agent_manifest("_shared/agents/researcher")
    builder = BootstrapBuilder(settings)
    bs = builder.build(
        run_id="run-no-cp",
        agent_spec=AgentSpec(id="researcher", ref="_shared/agents/researcher"),
        resolved_manifest=manifest, port=8100, peers={}, inputs={"topic": "x"},
    )
    assert "cp" not in bs


def test_bootstrap_carries_cp_block_when_configured() -> None:
    """With CP env configured, the bootstrap carries cp.base_url and
    cp.api_key so the agent's /admin/refresh-revocations route can call
    the CP without an explicit override."""
    settings = Settings(cp_base_url="http://cp.test:4000", cp_api_key="k")
    svc = RegistryService(settings)
    manifest = svc.get_agent_manifest("_shared/agents/researcher")
    builder = BootstrapBuilder(settings)
    bs = builder.build(
        run_id="run-with-cp",
        agent_spec=AgentSpec(id="researcher", ref="_shared/agents/researcher"),
        resolved_manifest=manifest, port=8100, peers={}, inputs={"topic": "x"},
    )
    # Exact equality on purpose: the CP block is the entire contract an agent
    # subprocess sees, so an accidental addition should fail here rather than
    # reach an agent unnoticed.
    assert bs["cp"] == {
        "base_url": "http://cp.test:4000",
        "api_key": "k",
        # Axis B policy. Agents never read Settings, so the revocation
        # freshness policy has to travel in this block or every agent
        # silently falls back to the constructor defaults.
        "fail_mode": "fail_closed",
        "max_staleness_secs": 300,
        "poll_secs": 60,
    }
