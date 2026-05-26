"""Pydantic models for scenario packs and agent manifests."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class PackMeta(BaseModel):
    slug: str
    name: str
    description: Optional[str] = None
    tags: list[str] = []


class Pack(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    kind: Literal["ScenarioPack"]
    metadata: PackMeta


class AgentSpec(BaseModel):
    id: str
    ref: str
    port_offset: int = 0
    org: Literal["internal", "external"] = "internal"
    cloud: Optional[str] = None
    did_web_host: Optional[str] = None


class TrustSpec(BaseModel):
    boundary: Literal["intra_org", "cross_org", "cross_cloud"]
    discovery: Literal["static", "cp_registry", "did_web"] = "static"
    # When false, the runner does NOT perform pairwise handshakes during setup —
    # scenarios that demonstrate trust gating or scoped grants need to control
    # exactly when (and with which grants) trust is established.
    eager: bool = True


WorkflowStepType = Literal[
    "workflow",                          # default — execute capability on step.agent
    "capability_call_no_trust",          # probe a capability without a TCT
    "capability_probe",                  # probe with held TCT — observes status code
    "handshake",                         # explicit handshake (initiator/responder)
    "revoke_tct",                        # add a peer's TCT jti to issuer's deny set
    "delegate",                          # delegator issues DelegationToken to delegatee
    "redeem_delegation",                 # delegatee presents token to original peer
    "rotate_keys",                       # agent replaces its keypair + republishes manifest
    "enroll_with_cp",                    # agent self-enrolls via CP /api/registry/enroll
]


FaultKind = Literal[
    "manifest_404",  # rewrite the peer's manifest URL to a path that 404s
    "peer_offline",  # rewrite the peer's host:port to an unbound port
]


class StepFault(BaseModel):
    """Operator-injected fault for a workflow step.

    Faults are pure-engine constructs: the runner intercepts the step,
    applies the transformation, and records the resulting outcome as a
    structured step output without bubbling the failure out of the run.
    Use them to demonstrate or test what happens when AITP plumbing
    breaks (peer drops, manifest 404, etc.) — the scenario continues so
    later steps can probe the consequences.
    """

    kind: FaultKind
    note: Optional[str] = None


class WorkflowStep(BaseModel):
    id: str
    type: Optional[WorkflowStepType] = None
    description: Optional[str] = None
    agent: Optional[str] = None
    capability: Optional[str] = None
    input_template: Optional[str] = None
    input_from: Optional[str] = None
    # capability_call_no_trust:
    target_agent: Optional[str] = None
    expect_status: Optional[int] = None
    # handshake:
    initiator: Optional[str] = None
    responder: Optional[str] = None
    requested_grants: Optional[list[str]] = None
    # revoke_tct:
    issuer: Optional[str] = None
    audience: Optional[str] = None
    # When true, also POST the jti to the Control Plane's
    # /api/revocation/entries and refresh the audience's view from
    # /.well-known/aitp-revocation-list, demonstrating end-to-end
    # propagation through the CP. Defaults false — pure-local revocation
    # (the original behavior) still works without a CP configured.
    via_cp: Optional[bool] = None
    reason: Optional[str] = None              # reason string passed to CP
    # delegate:
    delegator: Optional[str] = None
    delegatee: Optional[str] = None
    via_peer: Optional[str] = None            # the agent that issued delegator's held TCT
    scope: Optional[list[str]] = None
    ttl_secs: Optional[int] = None
    # redeem_delegation:
    via_delegation: Optional[str] = None      # id of the prior `delegate` step
    target: Optional[str] = None              # the agent whose redeem endpoint we POST to
    # Fault injection (applies to handshake / workflow / capability_probe).
    # When set, the runner mutates the call's target before issuing it
    # so the step exercises a failure path, and records the outcome in
    # step_outputs without raising the run.
    fault: Optional[StepFault] = None


class WorkflowSpec(BaseModel):
    steps: list[WorkflowStep] = []


class InputsSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")


class ScenarioSpec(BaseModel):
    inputs: InputsSpec = InputsSpec()
    agents: list[AgentSpec]
    trust: TrustSpec
    workflow: WorkflowSpec = WorkflowSpec()


class ScenarioMeta(BaseModel):
    pack: str
    scenario: str
    version: str
    name: str
    summary: Optional[str] = None
    tags: list[str] = []


class ScenarioVersion(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    kind: Literal["ScenarioVersion"]
    metadata: ScenarioMeta
    spec: ScenarioSpec


class AitpAgentSpec(BaseModel):
    offered_caps: list[str]
    display_name: str
    identity_type: Literal["pinned_key"] = "pinned_key"
    ttl_secs: int = 3600


class AgentManifestSpec(BaseModel):
    entrypoint: dict[str, Any]
    host: dict[str, Any] = Field(default_factory=dict)
    aitp: AitpAgentSpec
    did_web: bool = False


class AgentManifestMeta(BaseModel):
    id: str
    name: str
    framework: Literal["crewai", "langchain", "langgraph", "custom"]
    version: str = "1.0.0"


class AgentManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    kind: Literal["AgentManifest"]
    metadata: AgentManifestMeta
    spec: AgentManifestSpec
