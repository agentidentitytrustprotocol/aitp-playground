"""Phase 3 wiring tests — no aitp-py / no live CP required.

Covers the new CP-driven workflow step types and that the two new scenarios
load through the registry. End-to-end behavior against a live Control Plane is
covered by the docker-compose CP stack; here we lock in the model + load.
"""
from __future__ import annotations

from aitp_playground.config import Settings
from aitp_playground.registry.models import WorkflowStep
from aitp_playground.registry.service import RegistryService


def test_cp_step_types_are_valid() -> None:
    s1 = WorkflowStep(
        id="provision",
        type="cp_provision_trust_anchor",
        agent="writer",
        namespace="intra-org",
        issuer_url="https://issuer.demo.local",
    )
    assert s1.type == "cp_provision_trust_anchor"
    assert s1.issuer_url == "https://issuer.demo.local"

    s2 = WorkflowStep(id="tree", type="cp_delegation_tree", agent="researcher")
    assert s2.type == "cp_delegation_tree"


def test_trust_anchor_provisioning_scenario_loads() -> None:
    svc = RegistryService(Settings())
    sv = svc.get_scenario("intra-org/cp-trust-anchor-provisioning@1.0.0")
    assert sv.metadata.name == "CP Trust-Anchor Provisioning"
    provision = sv.spec.workflow.steps[0]
    assert provision.type == "cp_provision_trust_anchor"
    assert provision.agent == "writer"
    assert provision.namespace == "intra-org"
    assert provision.issuer_url == "https://issuer.demo.local"


def test_delegation_tree_scenario_loads() -> None:
    svc = RegistryService(Settings())
    sv = svc.get_scenario("intra-org/cp-delegation-tree@1.0.0")
    assert sv.metadata.name.startswith("CP Delegation Tree")
    assert {a.id for a in sv.spec.agents} == {"researcher", "writer", "sub-researcher"}
    last = sv.spec.workflow.steps[-1]
    assert last.type == "cp_delegation_tree"
    assert last.agent == "researcher"
    # The chain is actually built before we walk it.
    types = [s.type for s in sv.spec.workflow.steps]
    assert "delegate" in types and "redeem_delegation" in types
