"""Accepted identity types (RFC-AITP-0004 §5.1 step 8, aitp-rs #344).

A peer whose identity type is not in this agent's Manifest
`accepted_identity_types` is refused with INCOMPATIBLE_IDENTITY_TYPE. The
SDK defaults the list to the agent's own type, so a mixed oidc <-> pinned_key
pair works only when both sides list both. Agents take the list from their
bootstrap (`get_manifest_json`); scenarios set it per agent.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import aitp_server  # noqa: E402
import bootstrap as agent_bootstrap  # noqa: E402
from aitp_server import AitpServer  # noqa: E402
from oidc import OidcContext  # noqa: E402
from revocation_state import RevocationState  # noqa: E402

from aitp_playground.config import Settings  # noqa: E402
from aitp_playground.hosting.bootstrap import BootstrapBuilder  # noqa: E402
from aitp_playground.registry.service import RegistryService  # noqa: E402
from aitp_playground.registry.templates import apply_template  # noqa: E402
from aitp_playground.trust.oidc_issuer import RunOidcIssuer  # noqa: E402
from tests.unit.test_oidc import _oidc_bootstrap  # noqa: E402

MIXED = ["pinned_key", "oidc"]
OIDC_SCENARIO = "intra-org/oidc-identity@1.0.0"


def _oidc_initiator(issuer: RunOidcIssuer, accepted: list[str] | None = MIXED) -> "aitp.AitpAgent":
    a = aitp.AitpAgent.generate()
    kwargs: dict[str, Any] = {} if accepted is None else {"accepted_identity_types": accepted}
    a.build_manifest(
        "a", "https://a/aitp/handshake/hello", ["demo.x"],
        identity_type="oidc", oidc_issuer=issuer.issuer_url, oidc_subject="alice", **kwargs,
    )
    return a


def _hello(issuer: RunOidcIssuer, a: "aitp.AitpAgent", b: "aitp.AitpAgent", b_manifest: str):
    ctx = OidcContext(_oidc_bootstrap(issuer, subject="alice"))
    session = a.new_session(jwks=ctx.jwks, trust_anchors=ctx.trust_anchors, pinned_keys=[b.aid])
    responder = b.new_responder(jwks=ctx.jwks, trust_anchors=ctx.trust_anchors, pinned_keys=[a.aid])
    hello = session.build_hello(b_manifest, ["demo.y"], oidc_mint_jwt=ctx.mint_jwt_for(audience=b.aid, agent=a))
    return session, responder, hello


# ── the SDK check ───────────────────────────────────────────────────────


def test_a_pinned_only_responder_refuses_an_oidc_initiator() -> None:
    issuer = RunOidcIssuer.generate()
    a, b = _oidc_initiator(issuer), aitp.AitpAgent.generate()
    # build_hello refuses a peer whose Manifest does not accept this type, so
    # reach the responder's own check with a Manifest that does, then have the
    # responder publish its pinned-only one.
    b_mixed = b.build_manifest("b", "https://b/x", ["demo.y"], accepted_identity_types=MIXED)
    _session, _responder, hello = _hello(issuer, a, b, b_mixed)
    b.build_manifest("b", "https://b/x", ["demo.y"])
    ctx = OidcContext(_oidc_bootstrap(issuer, subject="alice"))
    responder = b.new_responder(jwks=ctx.jwks, trust_anchors=ctx.trust_anchors, pinned_keys=[a.aid])

    with pytest.raises(RuntimeError) as exc:
        responder.process_hello(hello)
    assert exc.value.code == "INCOMPATIBLE_IDENTITY_TYPE"


def test_an_oidc_only_initiator_refuses_a_pinned_responder() -> None:
    issuer = RunOidcIssuer.generate()
    a, b = _oidc_initiator(issuer, accepted=None), aitp.AitpAgent.generate()
    b_manifest = b.build_manifest("b", "https://b/x", ["demo.y"], accepted_identity_types=MIXED)
    session, responder, hello = _hello(issuer, a, b, b_manifest)
    ack, session_id = responder.process_hello(hello)

    with pytest.raises(RuntimeError) as exc:
        session.process_hello_ack(ack, session_id)
    assert exc.value.code == "INCOMPATIBLE_IDENTITY_TYPE"


def test_a_mixed_pair_listing_both_types_completes() -> None:
    issuer = RunOidcIssuer.generate()
    a, b = _oidc_initiator(issuer), aitp.AitpAgent.generate()
    b_manifest = b.build_manifest("b", "https://b/x", ["demo.y"], accepted_identity_types=MIXED)
    session, responder, hello = _hello(issuer, a, b, b_manifest)

    ack, session_id = responder.process_hello(hello)
    commit_ack, _ = responder.process_commit(session.process_hello_ack(ack, session_id), revoked_jtis=set())
    assert json.loads(session.complete(commit_ack, revoked_jtis=set()))["tct"]


# ── the agent ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(aitp_server, "emit_event", emit)


def _agent_bootstrap(**aitp_overrides: Any) -> dict[str, Any]:
    return {
        "run_id": "r", "agent_id": "b",
        "aitp": {
            "seed_hex": uuid.uuid4().hex * 2, "display_name": "b",
            "handshake_endpoint": "https://b/aitp/handshake/hello",
            "offered_caps": ["demo.y"], **aitp_overrides,
        },
    }


def _manifest_body(bootstrap: dict[str, Any]) -> dict[str, Any]:
    agent = agent_bootstrap.create_agent(bootstrap)
    return json.loads(agent_bootstrap.get_manifest_json(agent, bootstrap))["manifest"]


def test_an_agent_accepts_only_its_own_type_by_default() -> None:
    assert _manifest_body(_agent_bootstrap())["accepted_identity_types"] == ["pinned_key"]
    oidc = _agent_bootstrap(identity_type="oidc", oidc_issuer="https://idp/", oidc_subject="s")
    assert _manifest_body(oidc)["accepted_identity_types"] == ["oidc"]


def test_an_agent_publishes_the_types_its_bootstrap_lists() -> None:
    body = _manifest_body(_agent_bootstrap(accepted_identity_types=MIXED))
    assert body["accepted_identity_types"] == MIXED


async def test_a_pinned_only_agent_answers_an_oidc_hello_with_a_400() -> None:
    """The agent has the run issuer configured, as every agent in an OIDC
    run does, so the issuer is a trust anchor and only the type is refused."""
    issuer = RunOidcIssuer.generate()
    bootstrap = {**_agent_bootstrap(), "oidc": _oidc_bootstrap(issuer)["oidc"]}
    agent = agent_bootstrap.create_agent(bootstrap)
    a = _oidc_initiator(issuer)
    # build_hello refuses a peer whose Manifest does not accept this type, so
    # the HELLO is built against one that does; the server then publishes its own.
    mixed = agent_bootstrap.get_manifest_json(
        agent, {**bootstrap, "aitp": {**bootstrap["aitp"], "accepted_identity_types": MIXED}},
    )
    _session, _responder, hello = _hello(issuer, a, agent, mixed)
    server = AitpServer(
        agent=agent, manifest_json=agent_bootstrap.get_manifest_json(agent, bootstrap),
        port=9, bootstrap=bootstrap, revocation=RevocationState(),
    )
    server.trusted_peer_aids.add(a.aid)
    app = FastAPI()
    app.include_router(server.router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as b:
        r = await b.post("/aitp/handshake/hello", content=hello)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "INCOMPATIBLE_IDENTITY_TYPE"


# ── scenarios ───────────────────────────────────────────────────────────


def _accepted_by_agent(scenario: Any, svc: RegistryService) -> dict[str, Any]:
    builder = BootstrapBuilder(Settings(cp_base_url="", cp_api_key=""))
    return {
        spec.id: builder.build(
            run_id="r", agent_spec=spec, resolved_manifest=svc.get_agent_manifest(spec.ref),
            port=8100, peers={}, inputs={"topic": "x"},
        )["aitp"]["accepted_identity_types"]
        for spec in scenario.spec.agents
    }


def test_the_oidc_scenario_and_its_p256_variant_accept_both_types_on_both_sides() -> None:
    svc = RegistryService(Settings())
    base = svc.get_scenario(OIDC_SCENARIO)
    p256 = apply_template(base, svc.get_template(OIDC_SCENARIO, "p256-suite"))

    for scenario in (base, p256):
        assert _accepted_by_agent(scenario, svc) == {"researcher": MIXED, "writer": MIXED}


def test_an_agent_without_the_override_leaves_the_list_to_the_sdk_default() -> None:
    svc = RegistryService(Settings())
    accepted = _accepted_by_agent(svc.get_scenario("intra-org/research-and-write@1.0.0"), svc)
    assert set(accepted.values()) == {None}
