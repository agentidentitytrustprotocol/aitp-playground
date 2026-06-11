"""Offline validation of every shipped scenario pack.

These tests do NOT spawn agents or call LLMs — they load the on-disk registry
(the single source of truth, per CLAUDE.md) and assert that every scenario is
internally consistent: agents resolve to real manifests, every workflow step
references agents and capabilities that actually exist, step cross-references
point at real prior steps, and named templates apply cleanly.

This catches authoring mistakes (typo'd agent id, capability no agent offers,
dangling input_from) at unit-test speed, long before a live e2e run would.

The live, subprocess-spawning end-to-end variants live in
``tests/integration/`` (test_protocol_e2e.py / test_llm_e2e.py), gated behind
env vars.
"""
from __future__ import annotations

import pytest

from aitp_playground.config import Settings
from aitp_playground.registry.models import ScenarioVersion
from aitp_playground.registry.service import RegistryService

# Step fields whose value is an agent id (or list of agent ids).
_AGENT_ID_FIELDS = (
    "agent",
    "target_agent",
    "initiator",
    "responder",
    "delegator",
    "delegatee",
    "via_peer",
    "target",
    "coordinator",
    "verifier",
    "issuer",
    "audience",
)
_AGENT_ID_LIST_FIELDS = ("participants",)

# Step fields whose value is the id of another step in the same workflow.
_STEP_REF_FIELDS = ("input_from", "via_step", "via_delegation")


def _registry() -> RegistryService:
    # A long cache TTL loads the on-disk registry once for the whole module
    # instead of re-parsing every YAML on each call (the default TTL of 0 means
    # hot-reload, which makes this read-only validation suite needlessly slow).
    return RegistryService(Settings(registry_cache_ttl_ms=3_600_000))


_SVC = _registry()
_SCENARIOS = sorted(
    _SVC.list_scenarios(),
    key=lambda s: f"{s.metadata.pack}/{s.metadata.scenario}@{s.metadata.version}",
)
_REFS = [
    f"{s.metadata.pack}/{s.metadata.scenario}@{s.metadata.version}" for s in _SCENARIOS
]


def test_registry_has_scenarios() -> None:
    """Guard against the parametrization silently collecting nothing."""
    assert _SCENARIOS, "no scenarios discovered on disk"


@pytest.mark.parametrize("ref", _REFS)
def test_scenario_round_trips_through_registry(ref: str) -> None:
    """Every advertised ref loads back into a typed ScenarioVersion."""
    sv = _SVC.get_scenario(ref)
    assert isinstance(sv, ScenarioVersion)
    assert sv.spec.agents, f"{ref} declares no agents"


@pytest.mark.parametrize("ref", _REFS)
def test_agent_refs_resolve_to_manifests(ref: str) -> None:
    """Every agent in a scenario points at a manifest that loads, and agent
    ids within a scenario are unique."""
    sv = _SVC.get_scenario(ref)
    ids = [a.id for a in sv.spec.agents]
    assert len(ids) == len(set(ids)), f"{ref} has duplicate agent ids: {ids}"
    for agent in sv.spec.agents:
        manifest = _SVC.get_agent_manifest(agent.ref)
        assert manifest.spec.aitp.offered_caps, (
            f"{ref}: agent '{agent.id}' ({agent.ref}) offers no capabilities"
        )


@pytest.mark.parametrize("ref", _REFS)
def test_workflow_agent_references_exist(ref: str) -> None:
    """Every step field that names an agent names one declared by the scenario."""
    sv = _SVC.get_scenario(ref)
    known = {a.id for a in sv.spec.agents}
    for step in sv.spec.workflow.steps:
        for field in _AGENT_ID_FIELDS:
            value = getattr(step, field, None)
            if value is not None:
                assert value in known, (
                    f"{ref}: step '{step.id}' field '{field}={value}' "
                    f"is not a declared agent (have {sorted(known)})"
                )
        for field in _AGENT_ID_LIST_FIELDS:
            values = getattr(step, field, None) or []
            for value in values:
                assert value in known, (
                    f"{ref}: step '{step.id}' {field} entry '{value}' "
                    f"is not a declared agent (have {sorted(known)})"
                )


@pytest.mark.parametrize("ref", _REFS)
def test_workflow_step_cross_references_exist(ref: str) -> None:
    """input_from / via_step / via_delegation must point at an earlier step."""
    sv = _SVC.get_scenario(ref)
    seen: set[str] = set()
    for step in sv.spec.workflow.steps:
        for field in _STEP_REF_FIELDS:
            value = getattr(step, field, None)
            if value is not None:
                assert value in seen, (
                    f"{ref}: step '{step.id}' field '{field}={value}' "
                    f"does not reference an earlier step (seen {sorted(seen)})"
                )
        seen.add(step.id)


@pytest.mark.parametrize("ref", _REFS)
def test_step_capabilities_are_offered_by_some_agent(ref: str) -> None:
    """A step that requests a capability is satisfiable: at least one agent in
    the scenario offers it. (The runner routes to a holder via
    ScenarioRunner._find_capability_holder, so it need not be the named agent.)"""
    sv = _SVC.get_scenario(ref)
    offered: set[str] = set()
    for agent in sv.spec.agents:
        manifest = _SVC.get_agent_manifest(agent.ref)
        offered.update(manifest.spec.aitp.offered_caps)
    for step in sv.spec.workflow.steps:
        if step.capability is not None:
            assert step.capability in offered, (
                f"{ref}: step '{step.id}' needs capability "
                f"'{step.capability}' that no agent offers (have {sorted(offered)})"
            )


@pytest.mark.parametrize("ref", _REFS)
def test_step_ids_are_unique(ref: str) -> None:
    sv = _SVC.get_scenario(ref)
    ids = [s.id for s in sv.spec.workflow.steps]
    assert len(ids) == len(set(ids)), f"{ref} has duplicate step ids: {ids}"


@pytest.mark.parametrize("ref", _REFS)
def test_templates_apply_cleanly(ref: str) -> None:
    """Every template advertised for a scenario resolves into a valid merged
    ScenarioVersion (agents still resolve, references still hold)."""
    templates = _SVC.list_templates(ref)
    known_base = {a.id for a in _SVC.get_scenario(ref).spec.agents}
    for tmpl in templates:
        merged = _SVC.get_scenario_resolved(ref, template=tmpl.metadata.name)
        assert isinstance(merged, ScenarioVersion)
        merged_ids = {a.id for a in merged.spec.agents}
        # A template either keeps the base agents or fully replaces them; either
        # way every agent ref must still resolve.
        assert merged_ids, f"{ref} + template '{tmpl.metadata.name}' has no agents"
        for agent in merged.spec.agents:
            _SVC.get_agent_manifest(agent.ref)
        # Workflow agent refs in the merged scenario must still be declared.
        for step in merged.spec.workflow.steps:
            for field in _AGENT_ID_FIELDS:
                value = getattr(step, field, None)
                if value is not None:
                    assert value in merged_ids, (
                        f"{ref} + template '{tmpl.metadata.name}': step "
                        f"'{step.id}' field '{field}={value}' is not a declared "
                        f"agent (base had {sorted(known_base)})"
                    )
