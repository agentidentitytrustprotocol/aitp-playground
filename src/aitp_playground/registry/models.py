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
    # Per-agent overrides of the manifest defaults. ``signing_suite``
    # selects the signing algorithm passed to ``AitpAgent.from_seed``
    # (Ed25519 by default; ``"p256"`` for the ECDSA suite). When unset
    # the manifest's ``spec.aitp.signing_suite`` wins.
    signing_suite: Optional[Literal["ed25519", "p256"]] = None


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
    "cp_subscribe_webhook",              # register a CP webhook pointing at the run's receiver
    "renew_tct",                         # RFC-AITP-0005 §10 in-band TCT renewal
    "export_session_bundle",             # RFC-AITP-0010 build a SessionBundleEnvelope
    "verify_session_bundle",             # RFC-AITP-0010 verify a SessionBundleEnvelope
    "spki_pin_check",                    # compute_spki_hash + SpkiPinVerifier exercise
    "tct_cache_stats",                   # read an agent's RFC-AITP-0005 verify-cache counters
    "cp_provision_trust_anchor",         # push an OIDC issuer + pinned key to the CP, read back
    "cp_delegation_tree",                # walk a delegator's chain via CP /api/delegations
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
    # cp_subscribe_webhook:
    # Empty (or omitted) means "all deliverable event types" on the CP side
    # (agent.registered, handshake.complete, tct.revoked, etc.).
    events: Optional[list[str]] = None
    # renew_tct:
    # ``holder`` already lives in the standard agent / target_agent pair —
    # we use ``agent`` (holder) + ``via_peer`` (issuer) for symmetry with
    # the rest of the engine. Optional ``new_ttl_secs`` overrides the
    # issuer's default.
    new_ttl_secs: Optional[int] = None
    # export_session_bundle:
    # ``coordinator`` is the agent that builds the bundle from its issued
    # TCTs; ``participants`` lists the agents whose TCTs go into it.
    coordinator: Optional[str] = None
    participants: Optional[list[str]] = None
    # verify_session_bundle:
    # ``verifier`` is the agent verifying; ``via_step`` references an
    # earlier export_session_bundle step whose output we re-present.
    verifier: Optional[str] = None
    via_step: Optional[str] = None
    # spki_pin_check:
    # ``cert_der_b64`` is a base64-encoded leaf certificate; the step
    # computes its SPKI hash and asserts is_pinned against the inline
    # ``pins`` list (also base64-encoded 32-byte values).
    cert_der_b64: Optional[str] = None
    pins: Optional[list[str]] = None
    # cp_provision_trust_anchor:
    # ``namespace`` scopes the CP trust-anchor / pinned-key registration
    # (defaults to the pack slug). ``issuer_url`` is the OIDC issuer URL to
    # register as a trust anchor, when set (distinct from ``issuer``, which
    # the linter resolves as an agent id for revoke_tct).
    namespace: Optional[str] = None
    issuer_url: Optional[str] = None
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


# ── Scenario templates (variants) ───────────────────────────────────────────
#
# A template is a named override on top of a ScenarioVersion. It lives at
# ``<scenario>/<version>/templates/<name>.yaml`` and may replace
# ``trust`` (field-level patch), ``agents`` (full list replacement) and/or
# ``workflow.steps`` (full list replacement). Anything it omits falls
# through from the base scenario.
#
# The motivating use case is trust-strict / trust-relaxed / delegation
# variants of the same workflow: the participants and inputs are the
# same, only the trust posture and step sequence change.


class ScenarioTemplateMeta(BaseModel):
    name: str
    summary: Optional[str] = None


class ScenarioTemplateSpec(BaseModel):
    """All fields optional — present keys override, missing keys fall through.

    ``trust`` is merged at field level (a template can flip just ``eager``
    without restating ``boundary``). ``agents`` and ``workflow.steps``
    are full replacements: partial list patching has too many edge
    cases for a demo registry to be confident about.
    """

    trust: Optional[dict[str, Any]] = None
    agents: Optional[list[AgentSpec]] = None
    workflow: Optional[WorkflowSpec] = None


class ScenarioTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api_version: str = Field(alias="apiVersion")
    kind: Literal["ScenarioTemplate"]
    metadata: ScenarioTemplateMeta
    spec: ScenarioTemplateSpec = ScenarioTemplateSpec()


class AitpAgentSpec(BaseModel):
    offered_caps: list[str]
    display_name: str
    identity_type: Literal["pinned_key", "oidc"] = "pinned_key"
    # When identity_type == "oidc" the manifest is built with
    # IdentityHintKind::Oidc; the bootstrap JSON carries ``issuer`` /
    # ``subject`` so the agent can mint ID tokens via the in-process
    # mock issuer. Ignored for pinned_key.
    oidc_issuer: Optional[str] = None
    oidc_subject: Optional[str] = None
    # RFC-AITP-0001 §5.4 algorithm selector. Default Ed25519; ``"p256"``
    # selects the ECDSA suite. Per-scenario ``AgentSpec.signing_suite``
    # overrides this default.
    signing_suite: Literal["ed25519", "p256"] = "ed25519"
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
