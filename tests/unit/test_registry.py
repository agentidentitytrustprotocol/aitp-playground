"""Registry loader / service tests — no aitp-py required."""
from __future__ import annotations

from aitp_playground.config import Settings
from aitp_playground.registry.service import RegistryService


def test_loads_all_three_packs() -> None:
    svc = RegistryService(Settings())
    packs = {p.metadata.slug for p in svc.list_packs()}
    assert {"intra-org", "cross-org", "cross-cloud"}.issubset(packs)


def test_lists_all_scenarios() -> None:
    svc = RegistryService(Settings())
    refs = {
        f"{s.metadata.pack}/{s.metadata.scenario}@{s.metadata.version}"
        for s in svc.list_scenarios()
    }
    expected = {
        "intra-org/research-and-write@1.0.0",
        "intra-org/research-and-write@1.1.0",
        "cross-org/federated-analysis@1.0.0",
        "cross-cloud/distributed-review@1.0.0",
    }
    assert expected.issubset(refs)


def test_get_scenario_returns_typed_model() -> None:
    svc = RegistryService(Settings())
    sv = svc.get_scenario("intra-org/research-and-write@1.0.0")
    assert sv.metadata.name == "Research and Write"
    assert sv.spec.trust.boundary == "intra_org"
    assert {a.id for a in sv.spec.agents} == {"researcher", "writer"}


def test_get_agent_manifest_resolves_shared_ref() -> None:
    svc = RegistryService(Settings())
    m = svc.get_agent_manifest("_shared/agents/researcher")
    assert m.metadata.framework == "crewai"
    assert "research.query" in m.spec.aitp.offered_caps


def test_unknown_scenario_raises() -> None:
    svc = RegistryService(Settings())
    import pytest
    from aitp_playground.errors import ScenarioNotFoundError
    with pytest.raises(ScenarioNotFoundError):
        svc.get_scenario("nope/missing@0.0.0")
