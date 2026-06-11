"""TrustOrchestrator.resolve_peers — per-discovery-mode peer resolution.

These cover the branching logic (static / did_web / cp_registry, plus the
graceful-fallback paths) with the CP client and did:web resolver stubbed, so no
subprocess or network is needed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aitp_playground.config import Settings
from aitp_playground.trust import orchestrator as orch_mod
from aitp_playground.trust.orchestrator import TrustOrchestrator


def _scenario(*, discovery: str, agents: list[SimpleNamespace], steps=None) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(
            trust=SimpleNamespace(discovery=discovery),
            agents=agents,
            workflow=SimpleNamespace(steps=steps or []),
        )
    )


def _agent(agent_id: str, *, org: str = "internal", did_web_host: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=agent_id, org=org, did_web_host=did_web_host)


def _running(agent_id: str, port: int) -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, port=port)


class _FakeCp:
    def __init__(self, *, discovered: list[dict] | None = None, raises: bool = False) -> None:
        self._discovered = discovered or []
        self._raises = raises
        self.calls: list[str] = []

    async def discover_by_capability(self, capability: str) -> list[dict]:
        self.calls.append(capability)
        if self._raises:
            raise RuntimeError("CP unreachable")
        return self._discovered


def _orch(cp: _FakeCp) -> TrustOrchestrator:
    return TrustOrchestrator(cp, Settings())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# static discovery
# --------------------------------------------------------------------------- #


async def test_static_discovery_builds_localhost_manifest_urls() -> None:
    cp = _FakeCp()
    scenario = _scenario(discovery="static", agents=[_agent("alice"), _agent("bob")])
    running = {"alice": _running("alice", 8101), "bob": _running("bob", 8102)}

    peers = await _orch(cp).resolve_peers(scenario, running)

    assert peers["alice"] == {
        "manifest_url": "http://localhost:8101/.well-known/aitp-manifest",
        "did": None,
        "source": "static",
    }
    assert peers["bob"]["manifest_url"] == "http://localhost:8102/.well-known/aitp-manifest"
    assert cp.calls == []  # static never touches the CP


async def test_static_discovery_tolerates_missing_running_agent() -> None:
    cp = _FakeCp()
    scenario = _scenario(discovery="static", agents=[_agent("alice")])
    peers = await _orch(cp).resolve_peers(scenario, running={})
    assert peers["alice"]["manifest_url"] is None


# --------------------------------------------------------------------------- #
# did_web discovery
# --------------------------------------------------------------------------- #


async def test_did_web_resolves_via_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(did: str) -> str:
        assert did == "did:web:peer.example.com"
        return "https://peer.example.com/.well-known/aitp-manifest"

    monkeypatch.setattr(orch_mod, "resolve_did_web", fake_resolve)

    cp = _FakeCp()
    scenario = _scenario(
        discovery="did_web",
        agents=[_agent("ext", did_web_host="peer.example.com")],
    )
    peers = await _orch(cp).resolve_peers(scenario, {"ext": _running("ext", 8200)})

    assert peers["ext"] == {
        "manifest_url": "https://peer.example.com/.well-known/aitp-manifest",
        "did": "did:web:peer.example.com",
    }


async def test_did_web_encodes_colon_in_host(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    async def fake_resolve(did: str) -> str:
        seen["did"] = did
        return "http://localhost:9000/.well-known/aitp-manifest"

    monkeypatch.setattr(orch_mod, "resolve_did_web", fake_resolve)
    cp = _FakeCp()
    scenario = _scenario(
        discovery="did_web", agents=[_agent("ext", did_web_host="localhost:9000")]
    )
    await _orch(cp).resolve_peers(scenario, {"ext": _running("ext", 9000)})
    assert seen["did"] == "did:web:localhost%3A9000"


async def test_did_web_falls_back_to_localhost_on_resolve_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(did: str) -> str:
        raise RuntimeError("did host down")

    monkeypatch.setattr(orch_mod, "resolve_did_web", boom)
    cp = _FakeCp()
    scenario = _scenario(
        discovery="did_web", agents=[_agent("ext", did_web_host="peer.example.com")]
    )
    peers = await _orch(cp).resolve_peers(scenario, {"ext": _running("ext", 8201)})

    # Falls back to the local manifest URL but keeps the did label.
    assert peers["ext"]["manifest_url"] == "http://localhost:8201/.well-known/aitp-manifest"
    assert peers["ext"]["did"] == "did:web:peer.example.com"


# --------------------------------------------------------------------------- #
# cp_registry discovery
# --------------------------------------------------------------------------- #


async def test_cp_registry_uses_discovered_endpoint_for_external_agent() -> None:
    cp = _FakeCp(discovered=[{"handshake_endpoint": "https://ext.example.com/aitp/handshake/hello"}])
    scenario = _scenario(
        discovery="cp_registry",
        agents=[_agent("ext", org="external")],
        steps=[SimpleNamespace(agent="ext", capability="analyze.data")],
    )
    peers = await _orch(cp).resolve_peers(scenario, {"ext": _running("ext", 8300)})

    assert cp.calls == ["analyze.data"]  # cap hint pulled from the workflow
    assert peers["ext"]["manifest_url"] == "https://ext.example.com/.well-known/aitp-manifest"
    assert peers["ext"]["source"] == "cp_registry"


async def test_cp_registry_falls_back_when_no_agents_discovered() -> None:
    cp = _FakeCp(discovered=[])
    scenario = _scenario(
        discovery="cp_registry",
        agents=[_agent("ext", org="external")],
        steps=[SimpleNamespace(agent="ext", capability="analyze.data")],
    )
    peers = await _orch(cp).resolve_peers(scenario, {"ext": _running("ext", 8301)})

    assert peers["ext"]["manifest_url"] == "http://localhost:8301/.well-known/aitp-manifest"
    assert peers["ext"]["source"] == "static_fallback"


async def test_cp_registry_falls_back_when_cp_raises() -> None:
    cp = _FakeCp(raises=True)
    scenario = _scenario(
        discovery="cp_registry",
        agents=[_agent("ext", org="external")],
        steps=[SimpleNamespace(agent="ext", capability="analyze.data")],
    )
    peers = await _orch(cp).resolve_peers(scenario, {"ext": _running("ext", 8302)})
    assert peers["ext"]["source"] == "static_fallback"


async def test_cp_registry_internal_agent_uses_static_path() -> None:
    cp = _FakeCp(discovered=[{"handshake_endpoint": "https://nope/aitp/handshake/hello"}])
    scenario = _scenario(
        discovery="cp_registry",
        agents=[_agent("internal_one", org="internal")],
    )
    peers = await _orch(cp).resolve_peers(scenario, {"internal_one": _running("internal_one", 8303)})

    # Internal agents skip CP discovery entirely.
    assert cp.calls == []
    assert peers["internal_one"]["source"] == "static"


# --------------------------------------------------------------------------- #
# _cap_for_agent
# --------------------------------------------------------------------------- #


def test_cap_for_agent_returns_first_matching_capability() -> None:
    scenario = _scenario(
        discovery="static",
        agents=[],
        steps=[
            SimpleNamespace(agent="bob", capability=None),
            SimpleNamespace(agent="alice", capability="read.data"),
            SimpleNamespace(agent="alice", capability="write.data"),
        ],
    )
    assert TrustOrchestrator._cap_for_agent("alice", scenario) == "read.data"


def test_cap_for_agent_returns_none_when_no_step_matches() -> None:
    scenario = _scenario(
        discovery="static",
        agents=[],
        steps=[SimpleNamespace(agent="bob", capability="x")],
    )
    assert TrustOrchestrator._cap_for_agent("alice", scenario) is None
