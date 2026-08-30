"""Behavioral tests for ScenarioRunner.run() and its step dispatch.

No agent subprocess is ever spawned: a fake supervisor hands back ready
``RunningAgent`` records, and every agent-admin HTTP surface the engine
talks to is served by an ``httpx.MockTransport`` handler that replaces the
engine module's ``httpx`` reference. The registry, adapters, and CP client
are lightweight fakes; the port allocator, bootstrap builder, run store,
and trust orchestrator are the real implementations, so the tests observe
the engine's actual lifecycle side effects (port release, bootstrap file
cleanup, store finalization, CP event ingest).
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import aitp
import httpx
import pytest

from aitp_playground.config import Settings
from aitp_playground.errors import ScenarioNotFoundError
from aitp_playground.hosting.adapters.base import ManifestValidation
from aitp_playground.hosting.bootstrap import BootstrapBuilder
from aitp_playground.hosting.port_allocator import PortAllocator
from aitp_playground.hosting.supervisor import RunningAgent
from aitp_playground.registry.models import AgentManifest, ScenarioVersion, StepFault
from aitp_playground.runner import engine as engine_mod
from aitp_playground.runner.engine import ScenarioRunner
from aitp_playground.runner.store import RunStore
from aitp_playground.trust.orchestrator import TrustOrchestrator

SCENARIO_REF = "test/demo@1.0.0"

# Self-signed Ed25519 cert + matching SPKI pin from the shipped
# intra-org/spki-pinning scenario (known-good pair computed offline).
_SPKI_CERT_B64 = (
    "MIHqMIGdoAMCAQICAQEwBQYDK2VwMB8xHTAbBgNVBAMMFGFpdHAtcGxheWdyb3VuZC10ZXN0"
    "MB4XDTI1MDEwMTAwMDAwMFoXDTM1MDEwMTAwMDAwMFowHzEdMBsGA1UEAwwUYWl0cC1wbGF5"
    "Z3JvdW5kLXRlc3QwKjAFBgMrZXADIQADoQe/884Qvh1w3RjnS8CZZ+TWMJulDV8d3IZkElUx"
    "uDAFBgMrZXADQQBf7eAcMWnzNUS7/K6nk22d1fJX7vE/2e0EnW6KEb7LCBrIwvavlKomxu5o"
    "NStataOrBnnsS4PgKTMU/ItJlPUP"
)
_SPKI_MATCHING_PIN = "oFCDfYUHBYLM9zlLCYiEfMMSy4glm4lImfbyOc8XkaU="
_SPKI_WRONG_PIN = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


_FAKE_AGENT_MANIFESTS: dict[int, str] = {}


def _fake_agent_manifest(port: int) -> str:
    """A genuinely signed ManifestEnvelope for a fake agent on `port`.

    Cached per port so repeat fetches are byte-stable within a run.
    """
    cached = _FAKE_AGENT_MANIFESTS.get(port)
    if cached is None:
        agent = aitp.AitpAgent.generate()
        cached = agent.build_manifest(
            display_name=f"fake-{port}",
            handshake_endpoint=f"http://localhost:{port}/aitp/handshake/hello",
            offered_caps=["cap.a"],
        )
        _FAKE_AGENT_MANIFESTS[port] = cached
    return cached


class _AgentHttpHandler:
    """Default in-memory implementation of every agent /admin surface the
    engine calls. Tests override individual paths via ``overrides``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        override = self.overrides.get(request.url.path)
        if override is not None:
            return override(request)
        return self._default(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def body_of(self, path: str) -> dict[str, Any]:
        for r in self.requests:
            if r.url.path == path:
                return json.loads(r.content)
        raise AssertionError(f"no request seen for {path}; saw {self.paths()}")

    def _default(self, request: httpx.Request) -> httpx.Response:  # noqa: C901
        path = request.url.path
        port = request.url.port
        if path == "/admin/initiate-handshake":
            body = json.loads(request.content)
            if "-DOES-NOT-EXIST" in body["peer_manifest_url"]:
                return httpx.Response(502, json={"detail": "manifest fetch failed"})
            return httpx.Response(200, json={
                "grants": list(body.get("requested_grants") or []) or ["cap.default"],
                "jti": f"jti-{port}",
            })
        if path == "/admin/invoke":
            body = json.loads(request.content)
            if body.get("peer_port") == 1:
                # peer_offline fault — caller holds no TCT for the mutated port.
                return httpx.Response(200, json={
                    "error": True, "status_code": 412, "body": "no TCT for peer",
                })
            return httpx.Response(200, json={"invoked": body["capability"]})
        if path == "/admin/self-execute":
            body = json.loads(request.content)
            return httpx.Response(200, json={"self_executed": body["capability"]})
        if path.startswith("/capabilities/"):
            return httpx.Response(401, text="trust required")
        if path == "/admin/delegate":
            return httpx.Response(200, json={
                "delegation_token": "DT-1", "delegatee_aid": "aid-delegatee",
            })
        if path == "/admin/redeem-delegation":
            return httpx.Response(200, json={"redeemed": True})
        if path == "/admin/revoke-tct":
            return httpx.Response(200, json={"revoked": True})
        if path == "/admin/refresh-revocations":
            return httpx.Response(200, json={"revoked_count": 2})
        if path == "/admin/enroll-with-cp":
            return httpx.Response(200, json={
                "aid": "aid-enrolled", "registered_at": "2026-07-07T00:00:00Z",
            })
        if path == "/.well-known/aitp-manifest":
            # A REAL minted manifest, not a hand-built dict: the engine now
            # verifies this envelope before reading key material out of it
            # (cp_provision_trust_anchor pins the key into the CP's trust
            # store). A fabricated manifest no longer verifies — which is the
            # interlock working, so the fixture mints instead of relaxing it.
            return httpx.Response(200, text=_fake_agent_manifest(port))
        if path == "/admin/renew-tct":
            return httpx.Response(200, json={"jti": "jti-renewed", "expires_at": 4102444800})
        if path == "/admin/tct-cache-stats":
            return httpx.Response(200, json={"hits": 5, "misses": 1, "entries": 1})
        if path == "/admin/held-tct":
            return httpx.Response(200, json={"aid": f"aid-port-{port}", "tct_token": f"tok-{port}"})
        if path == "/admin/export-session-bundle":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "bundle_envelope": {"payload": "bundle"},
                "participant_aids": [p["aid"] for p in body["participant_tcts"]],
                "session_id": "sess-1",
            })
        if path == "/admin/verify-session-bundle":
            return httpx.Response(200, json={
                "kind": "session_bundle", "active_aids": ["aid-1"], "dropped_aids": [],
            })
        if path == "/admin/rotate-keys":
            return httpx.Response(200, json={"aid": "aid-rotated"})
        return httpx.Response(404, json={"unhandled": path})


@pytest.fixture()
def agent_http(monkeypatch) -> _AgentHttpHandler:
    """Route the engine's httpx.AsyncClient through the fake agent handler.

    Only the engine module's ``httpx`` reference is swapped, so the CP
    client and everything else keep the real library."""
    handler = _AgentHttpHandler()
    transport = httpx.MockTransport(handler)

    def _client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(engine_mod, "httpx", SimpleNamespace(AsyncClient=_client))
    return handler


class FakeRegistry:
    def __init__(self, scenarios: dict[str, ScenarioVersion], manifests: dict[str, AgentManifest]) -> None:
        self.scenarios = scenarios
        self.manifests = manifests

    def get_scenario_resolved(self, ref: str, template: Optional[str] = None) -> ScenarioVersion:
        try:
            return self.scenarios[ref]
        except KeyError:
            raise ScenarioNotFoundError(f"scenario not found: {ref}") from None

    def get_agent_manifest(self, ref: str) -> AgentManifest:
        return self.manifests[ref]


class FakeSupervisor:
    def __init__(self, fail_on: Optional[str] = None) -> None:
        self.fail_on = fail_on
        self.launched: list[str] = []
        self.agents: dict[str, RunningAgent] = {}
        self.killed: list[str] = []

    async def launch(self, *, run_id: str, agent_id: str, prepared: Any, port: int,
                     startup_timeout_ms: int = 30_000) -> RunningAgent:
        if agent_id == self.fail_on:
            raise RuntimeError(f"launch failed for {agent_id}")
        ra = RunningAgent(
            run_id=run_id, agent_id=agent_id, port=port, pid=None,
            # The AID the real supervisor reads from the agent's own
            # AITP_AGENT_READY line. Taken from the same cached fixture the
            # manifest endpoint serves, so the two agree by construction —
            # cp_provision_trust_anchor now asserts exactly that.
            aid=json.loads(_fake_agent_manifest(port))["manifest"]["aid"],
            manifest_url=f"http://localhost:{port}/.well-known/aitp-manifest",
            status="ready",
        )
        self.launched.append(agent_id)
        self.agents[agent_id] = ra
        return ra

    def kill_run(self, run_id: str) -> None:
        self.killed.append(run_id)


class FakeAdapter:
    def __init__(self, errors: Optional[list[str]] = None) -> None:
        self.errors = list(errors or [])

    def validate(self, manifest: AgentManifest) -> ManifestValidation:
        return ManifestValidation(valid=not self.errors, errors=self.errors)

    def prepare_launch(self, manifest: AgentManifest, bootstrap_file: str, port: int, config: Settings) -> Any:
        return SimpleNamespace(command="noop", args=[], env={}, cwd=".")


class FakeAdapters:
    def __init__(self, errors: Optional[list[str]] = None) -> None:
        self._adapter = FakeAdapter(errors)

    def get(self, framework: str) -> FakeAdapter:
        return self._adapter


class FakeCp:
    def __init__(self, enabled: bool = False, *, delegations: Optional[list] = None,
                 webhook: Optional[dict] = None) -> None:
        self._enabled = enabled
        self.ingested: list[list[Any]] = []
        self.revocations: list[tuple[str, str]] = []
        self.webhook = webhook
        self.delegations = list(delegations or [])
        self.pinned: list[dict[str, Any]] = []
        self.anchors: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def ingest_events(self, events: list[Any]) -> None:
        self.ingested.append(list(events or []))

    async def publish_revocation(self, jti: str, reason: str = "") -> bool:
        self.revocations.append((jti, reason))
        return True

    async def create_webhook(self, *, url: str, events: Optional[list[str]] = None,
                             **_: Any) -> Optional[dict[str, Any]]:
        return self.webhook

    async def fetch_delegations(self, **_: Any) -> list[dict[str, Any]]:
        return list(self.delegations)

    async def upsert_pinned_key(self, **kw: Any) -> Optional[dict[str, Any]]:
        self.pinned.append(kw)
        return {"id": "pk-1", **kw}

    async def upsert_trust_anchor(self, **kw: Any) -> Optional[dict[str, Any]]:
        self.anchors.append(kw)
        return {"id": "ta-1", **kw}

    async def list_pinned_keys(self, *, namespace: Optional[str] = None) -> list[dict[str, Any]]:
        return list(self.pinned)

    async def list_trust_anchors(self, *, namespace: Optional[str] = None) -> list[dict[str, Any]]:
        return list(self.anchors)

    async def discover_by_capability(self, capability: str) -> list[dict[str, Any]]:
        return []


# --------------------------------------------------------------------------- #
# environment builder
# --------------------------------------------------------------------------- #


def _agent_manifest(agent_id: str, caps: list[str], identity_type: str = "pinned_key") -> AgentManifest:
    aitp_block: dict[str, Any] = {
        "offered_caps": list(caps),
        "display_name": agent_id.title(),
        "identity_type": identity_type,
    }
    if identity_type == "oidc":
        aitp_block["oidc_issuer"] = "http://issuer.local"
        aitp_block["oidc_subject"] = agent_id
    return AgentManifest.model_validate({
        "apiVersion": "aitp.dev/v1",
        "kind": "AgentManifest",
        "metadata": {"id": agent_id, "name": agent_id, "framework": "custom"},
        "spec": {
            "entrypoint": {"type": "python_module", "value": f"agents.{agent_id}"},
            "host": {},
            "aitp": aitp_block,
        },
    })


def make_env(
    *,
    agents: dict[str, list[str]],
    steps: list[dict[str, Any]],
    eager: bool = True,
    cp: Optional[FakeCp] = None,
    adapters: Optional[FakeAdapters] = None,
    supervisor: Optional[FakeSupervisor] = None,
    identity_types: Optional[dict[str, str]] = None,
) -> SimpleNamespace:
    cp = cp or FakeCp()
    settings = Settings(
        cp_base_url="http://cp.test" if cp.enabled else "",
        cp_api_key="",
    )
    manifests: dict[str, AgentManifest] = {}
    agent_specs: list[dict[str, Any]] = []
    for idx, (aid, caps) in enumerate(agents.items()):
        ref = f"_test/{aid}"
        manifests[ref] = _agent_manifest(
            aid, caps, (identity_types or {}).get(aid, "pinned_key"),
        )
        agent_specs.append({"id": aid, "ref": ref, "port_offset": idx})
    scenario = ScenarioVersion.model_validate({
        "apiVersion": "aitp.dev/v1",
        "kind": "ScenarioVersion",
        "metadata": {
            "pack": "test", "scenario": "demo", "version": "1.0.0", "name": "Demo",
        },
        "spec": {
            "inputs": {"schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            }},
            "agents": agent_specs,
            "trust": {"boundary": "intra_org", "discovery": "static", "eager": eager},
            "workflow": {"steps": steps},
        },
    })
    supervisor = supervisor or FakeSupervisor()
    ports = PortAllocator(start=9500)
    store = RunStore()
    runner = ScenarioRunner(
        registry=FakeRegistry({SCENARIO_REF: scenario}, manifests),
        supervisor=supervisor,
        bootstrap_builder=BootstrapBuilder(settings),
        adapters=adapters or FakeAdapters(),
        trust=TrustOrchestrator(cp, settings),
        cp=cp,
        port_alloc=ports,
        config=settings,
        store=store,
        )
    return SimpleNamespace(
        runner=runner, supervisor=supervisor, ports=ports, store=store,
        cp=cp, scenario=scenario,
    )


async def _run(env: SimpleNamespace, *, inputs: Optional[dict[str, Any]] = None,
               run_id: str = "run-1", ref: str = SCENARIO_REF):
    result = await env.runner.run(
        scenario_ref=ref,
        inputs={"topic": "t"} if inputs is None else inputs,
        run_id=run_id,
    )
    # Drain the fire-and-forget CP ingest task so assertions are deterministic.
    if env.runner._bg_tasks:
        await asyncio.gather(*list(env.runner._bg_tasks))
    return result


def _event_types(result) -> list[str]:
    return [e.type for e in result.events]


def _bootstrap_files(run_id: str) -> list[Path]:
    d = Path(tempfile.gettempdir()) / "aitp-bootstrap"
    return sorted(d.glob(f"{run_id}_*.json")) if d.exists() else []


# --------------------------------------------------------------------------- #
# run lifecycle
# --------------------------------------------------------------------------- #


async def test_successful_run_full_lifecycle(agent_http) -> None:
    """Two-agent workflow: self-execute + cross-agent invoke, eager trust.
    Verifies event sequence, outputs, store finalization, cleanup, and the
    post-run CP event ingest."""
    env = make_env(
        agents={"alice": ["research.query"], "bob": ["write.content"]},
        steps=[
            {"id": "research", "agent": "alice", "capability": "research.query",
             "input_template": "topic: {{ inputs.topic }}"},
            {"id": "write", "agent": "alice", "capability": "write.content",
             "input_from": "research"},
        ],
    )
    result = await _run(env, inputs={"topic": "AITP"})

    assert result.status == "success", result.error
    assert set(result.outputs) == {"research", "write"}
    # alice offers research.query herself → self-execute; write.content lives
    # on bob → cross-agent /admin/invoke.
    assert result.outputs["research"] == {"self_executed": "research.query"}
    assert result.outputs["write"] == {"invoked": "write.content"}

    types = _event_types(result)
    for expected in ("run.started", "agent.spawning", "agent.ready",
                     "trust.peers_resolved", "trust.established",
                     "step.started", "step.complete", "run.complete"):
        assert expected in types, f"missing {expected}: {types}"
    # Eager trust between 2 agents → both directions handshaked.
    assert types.count("trust.established") == 2

    # Store finalized as success with the outputs.
    record = env.store.get("run-1")
    assert record["status"] == "success"
    assert record["outputs"]["write"] == {"invoked": "write.content"}

    # Cleanup: subprocesses killed, bootstrap files removed, ports released
    # (both agent ports are recycled instead of the allocator advancing).
    assert env.supervisor.killed == ["run-1"]
    assert _bootstrap_files("run-1") == []
    assert {env.ports.allocate(), env.ports.allocate()} == {9500, 9501}

    # Post-run CP ingest received the full store event log (superset of
    # orchestrator events).
    assert len(env.cp.ingested) == 1
    ingested_types = {e.get("type") for e in env.cp.ingested[0]}
    assert {"run.started", "run.complete"} <= ingested_types


async def test_unknown_scenario_fails_before_spawn(agent_http) -> None:
    env = make_env(agents={"alice": ["cap.a"]}, steps=[])
    result = await _run(env, ref="nope/missing@1.0.0")
    assert result.status == "failed"
    assert "scenario not found" in result.error
    assert env.supervisor.launched == []
    assert env.store.get("run-1")["status"] == "failed"
    assert _event_types(result) == ["run.failed"]


async def test_input_schema_violation_fails_before_spawn(agent_http) -> None:
    env = make_env(agents={"alice": ["cap.a"]}, steps=[])
    result = await _run(env, inputs={})  # schema requires "topic"
    assert result.status == "failed"
    assert result.error.startswith("inputs validation:")
    assert env.supervisor.launched == []
    assert env.store.get("run-1")["error"].startswith("inputs validation:")


async def test_adapter_validation_failure_fails_run_and_cleans_up(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, steps=[],
        adapters=FakeAdapters(errors=["missing entrypoint"]),
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "manifest invalid for alice" in result.error
    # Cleanup runs in both the except and finally paths — idempotent by design.
    assert "run-1" in env.supervisor.killed
    assert _bootstrap_files("run-1") == []
    assert env.ports.allocate() in (9500, 9501)  # port went back to the pool


async def test_supervisor_launch_failure_fails_run_and_cleans_up(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, steps=[],
        supervisor=FakeSupervisor(fail_on="bob"),
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "launch failed for bob" in result.error
    # alice launched first, then the whole run was killed.
    assert env.supervisor.launched == ["alice"]
    assert "run-1" in env.supervisor.killed
    assert _bootstrap_files("run-1") == []
    assert "run.failed" in _event_types(result)


async def test_oidc_agents_get_a_per_run_issuer(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, steps=[], eager=False,
        identity_types={"alice": "oidc"},
    )
    result = await _run(env)
    assert result.status == "success", result.error
    minted = [e for e in result.events if e.type == "oidc.issuer_minted"]
    assert len(minted) == 1
    assert minted[0].result["issuer_url"]
    assert minted[0].result["kid"]


# --------------------------------------------------------------------------- #
# workflow / capability steps
# --------------------------------------------------------------------------- #


async def test_meta_step_is_skipped_without_output(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "narrative", "description": "just a note"}],
    )
    result = await _run(env)
    assert result.status == "success"
    assert "narrative" not in result.outputs
    skipped = [e for e in result.events if e.type == "step.skipped"]
    assert skipped and skipped[0].step_id == "narrative"


async def test_workflow_step_with_unoffered_capability_fails_run(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "s1", "agent": "alice", "capability": "ghost.cap"}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "No agent in scenario offers capability ghost.cap" in result.error


async def test_explicit_workflow_step_requires_capability(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "s1", "type": "workflow", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "workflow step requires agent and capability" in result.error


async def test_peer_rejection_on_workflow_invoke_fails_run(agent_http) -> None:
    """/admin/invoke wrapping an inner 4xx is a hard failure for a normal
    workflow step — the chain cannot continue on a rejection."""
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]},
        steps=[{"id": "w", "agent": "alice", "capability": "write.content"}],
    )
    agent_http.overrides["/admin/invoke"] = lambda req: httpx.Response(
        200, json={"error": True, "status_code": 403, "body": {"reason": "scope"}},
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "peer rejected write.content" in result.error
    assert "403" in result.error


async def test_invoke_and_self_execute_tolerate_non_json_bodies(agent_http) -> None:
    env = make_env(
        agents={"alice": ["research.query"], "bob": ["write.content"]},
        steps=[
            {"id": "research", "agent": "alice", "capability": "research.query"},
            {"id": "write", "agent": "alice", "capability": "write.content"},
        ],
    )
    agent_http.overrides["/admin/self-execute"] = lambda req: httpx.Response(200, text="plain research")
    agent_http.overrides["/admin/invoke"] = lambda req: httpx.Response(200, text="plain write")
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["research"] == {"raw": "plain research"}
    assert result.outputs["write"] == {"raw": "plain write"}


async def test_capability_call_no_trust_records_rejection(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]}, eager=False,
        steps=[{"id": "probe", "type": "capability_call_no_trust",
                "agent": "alice", "target_agent": "bob",
                "capability": "write.content", "expect_status": 401}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["probe"]
    assert out["status_code"] == 401
    assert out["rejected"] is True
    assert out["matched"] is True
    assert out["body"] == "trust required"  # non-JSON body falls back to text
    assert "step.access_denied" in _event_types(result)
    # The probe hits the target's capability endpoint directly, w/o a TCT.
    assert any(p == "/capabilities/write.content" for p in agent_http.paths())


async def test_capability_call_no_trust_flags_unexpected_status(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]}, eager=False,
        steps=[{"id": "probe", "type": "capability_call_no_trust",
                "agent": "alice", "target_agent": "bob",
                "capability": "write.content", "expect_status": 401}],
    )
    # Misconfigured peer answers 200 where a 401 was expected.
    agent_http.overrides["/capabilities/write.content"] = lambda req: httpx.Response(
        200, json={"ok": True},
    )
    result = await _run(env)
    assert result.status == "success"
    out = result.outputs["probe"]
    assert out["status_code"] == 200
    assert out["rejected"] is False
    assert out["matched"] is False
    assert "step.unexpected_status" in _event_types(result)


async def test_capability_call_no_trust_requires_target_fields(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "probe", "type": "capability_call_no_trust", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "capability_call_no_trust requires agent, target_agent, capability" in result.error


async def test_capability_probe_records_denied_status_without_failing(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]},
        steps=[{"id": "probe", "type": "capability_probe",
                "agent": "alice", "target_agent": "bob",
                "capability": "write.content", "expect_status": 403}],
    )
    agent_http.overrides["/admin/invoke"] = lambda req: httpx.Response(
        200, json={"error": True, "status_code": 403, "body": {"reason": "out of scope"}},
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["probe"]
    assert out == {
        "status_code": 403, "rejected": True, "expected_status": 403,
        "matched": True, "body": {"reason": "out of scope"},
    }
    assert "step.access_denied" in _event_types(result)


async def test_capability_probe_success_counts_as_200(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]},
        steps=[{"id": "probe", "type": "capability_probe",
                "agent": "alice", "target_agent": "bob",
                "capability": "write.content"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["probe"]
    assert out["status_code"] == 200
    assert out["rejected"] is False
    assert out["matched"] is True
    assert out["body"] == {"invoked": "write.content"}


# --------------------------------------------------------------------------- #
# handshake / delegation / revocation steps
# --------------------------------------------------------------------------- #


async def test_handshake_step_runs_only_the_explicit_direction(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]}, eager=False,
        steps=[{"id": "hs", "type": "handshake", "initiator": "alice",
                "responder": "bob", "requested_grants": ["write.content"]}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["hs"] == {"trust": "established"}
    handshakes = [r for r in agent_http.requests if r.url.path == "/admin/initiate-handshake"]
    assert len(handshakes) == 1  # no auto reverse direction
    assert handshakes[0].url.port == 9500  # posted to the initiator (alice)
    body = json.loads(handshakes[0].content)
    assert body["peer_manifest_url"].endswith(":9501/.well-known/aitp-manifest")
    assert body["requested_grants"] == ["write.content"]
    established = [e for e in result.events if e.type == "trust.established"]
    assert len(established) == 1
    assert established[0].grants == ["write.content"]


async def test_handshake_step_requires_initiator_and_responder(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, eager=False,
        steps=[{"id": "hs", "type": "handshake", "initiator": "alice"}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "handshake requires initiator and responder" in result.error


async def test_delegate_then_redeem_flow(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"], "carol": ["cap.c"]},
        steps=[
            {"id": "del", "type": "delegate", "delegator": "alice",
             "delegatee": "carol", "via_peer": "bob",
             "scope": ["cap.b"], "ttl_secs": 60},
            {"id": "redeem", "type": "redeem_delegation", "delegatee": "carol",
             "target": "bob", "via_delegation": "del"},
        ],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["del"]["delegation_token"] == "DT-1"
    assert result.outputs["redeem"] == {"redeemed": True}

    delegate_body = agent_http.body_of("/admin/delegate")
    assert delegate_body["held_tct_peer_port"] == 9501  # bob issued the held TCT
    assert delegate_body["scope"] == ["cap.b"]
    assert delegate_body["ttl_secs"] == 60
    assert delegate_body["delegatee_manifest_url"].endswith(":9502/.well-known/aitp-manifest")

    redeem_body = agent_http.body_of("/admin/redeem-delegation")
    assert redeem_body["delegation_token"] == "DT-1"
    assert redeem_body["redeem_url"].endswith(":9501/aitp/delegation/redeem")
    assert redeem_body["peer_port"] == 9501

    types = _event_types(result)
    assert "delegation.issuing" in types
    assert "delegation.redeeming" in types


async def test_delegate_requires_non_empty_scope(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"], "carol": ["cap.c"]},
        steps=[{"id": "del", "type": "delegate", "delegator": "alice",
                "delegatee": "carol", "via_peer": "bob", "scope": []}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "delegate requires non-empty scope" in result.error


async def test_redeem_without_prior_delegation_token_fails(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]},
        steps=[
            {"id": "note", "description": "meta step, no token"},
            {"id": "redeem", "type": "redeem_delegation", "delegatee": "alice",
             "target": "bob", "via_delegation": "note"},
        ],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "did not produce a delegation_token" in result.error


async def test_revoke_tct_local_only(agent_http) -> None:
    """Eager trust records trust.established with a jti; revoke_tct walks
    the event log back to the TCT bob issued to alice."""
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]},
        steps=[{"id": "rev", "type": "revoke_tct", "issuer": "bob", "audience": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    # alice initiated toward bob from port 9500 → jti-9500.
    assert result.outputs["rev"] == {"revoked_jti": "jti-9500"}
    revoke = agent_http.body_of("/admin/revoke-tct")
    assert revoke == {"jti": "jti-9500"}
    # Issuer-side revoke only — no CP publication requested.
    assert env.cp.revocations == []


async def test_revoke_tct_via_cp_publishes_and_refreshes_audience(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]},
        cp=FakeCp(enabled=True),
        steps=[{"id": "rev", "type": "revoke_tct", "issuer": "bob",
                "audience": "alice", "via_cp": True, "reason": "compromised"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["rev"]
    assert out["revoked_jti"] == "jti-9500"
    assert out["published_to_cp"] is True
    assert out["audience_revoked_count"] == 2
    assert env.cp.revocations == [("jti-9500", "compromised")]
    assert "revocation.published" in _event_types(result)
    # The audience (alice, port 9500) is told to refresh from the CP.
    refresh = agent_http.body_of("/admin/refresh-revocations")
    assert refresh["cp_base_url"] == "http://cp.test"


async def test_revoke_tct_without_prior_handshake_fails(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, eager=False,
        steps=[{"id": "rev", "type": "revoke_tct", "issuer": "bob", "audience": "alice"}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "handshake first" in result.error


# --------------------------------------------------------------------------- #
# CP-optional steps: every one degrades to step.skipped without a CP
# --------------------------------------------------------------------------- #


async def test_enroll_with_cp_skips_gracefully_without_cp(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "enroll", "type": "enroll_with_cp", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["enroll"] == {"enrolled": False, "skipped": "no cp"}
    assert "step.skipped" in _event_types(result)
    assert not any(p == "/admin/enroll-with-cp" for p in agent_http.paths())


async def test_enroll_with_cp_runs_agent_side_enrollment(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False, cp=FakeCp(enabled=True),
        steps=[{"id": "enroll", "type": "enroll_with_cp", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["enroll"]["aid"] == "aid-enrolled"
    types = _event_types(result)
    assert "cp.enroll_started" in types
    assert "cp.enroll_complete" in types


async def test_cp_subscribe_webhook_skips_without_cp(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "sub", "type": "cp_subscribe_webhook"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["sub"] == {"subscribed": False, "skipped": "no cp"}


async def test_cp_subscribe_webhook_stores_subscription(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        cp=FakeCp(enabled=True, webhook={"id": "wh-1", "secret": "sh"}),
        steps=[{"id": "sub", "type": "cp_subscribe_webhook",
                "events": ["tct.revoked"]}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["sub"]
    assert out["subscribed"] is True
    assert out["webhook_id"] == "wh-1"
    assert out["url"].endswith("/webhooks/cp/run-1")
    assert out["events"] == ["tct.revoked"]
    # The webhook (incl. secret) is persisted on the run record for the
    # signature-verifying receiver.
    assert env.store.get("run-1")["cp_webhook"]["secret"] == "sh"
    assert "cp.webhook.subscribed" in _event_types(result)


async def test_cp_subscribe_webhook_records_refusal_without_failing(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        cp=FakeCp(enabled=True, webhook=None),
        steps=[{"id": "sub", "type": "cp_subscribe_webhook"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["sub"] == {"subscribed": False}
    assert "cp.webhook.subscribe_failed" in _event_types(result)


async def test_cp_provision_trust_anchor_skips_without_cp(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "prov", "type": "cp_provision_trust_anchor", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["prov"] == {"provisioned": False, "skipped": "no cp"}


async def test_cp_provision_trust_anchor_registers_key_and_issuer(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False, cp=FakeCp(enabled=True),
        steps=[{"id": "prov", "type": "cp_provision_trust_anchor",
                "agent": "alice", "issuer_url": "https://issuer.test"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["prov"]
    assert out["provisioned"] is True
    assert out["namespace"] == "test"  # defaults to the pack slug
    assert out["pinned_key_count"] == 1
    assert out["trust_anchor_count"] == 1
    # The pinned key comes from the agent's published manifest — which the
    # engine now verifies before reading it. Assert it against the manifest
    # actually served rather than a hard-coded literal, so the test cannot
    # drift from the fixture.
    served = json.loads(_fake_agent_manifest(env.supervisor.agents["alice"].port))
    assert env.cp.pinned[0]["aid"] == served["manifest"]["aid"]
    assert env.cp.pinned[0]["pubkey"] == served["manifest"]["identity_hint"]["public_key"]
    assert env.cp.anchors[0]["issuer_url"] == "https://issuer.test"
    assert "cp.trust_anchor.provisioned" in _event_types(result)


async def test_cp_provision_refuses_a_manifest_from_a_different_agent(agent_http) -> None:
    """The substitution the identity pin exists to stop.

    Verification alone is not enough here: this step pins the key it finds
    into the control plane's trust store **under `ra.aid`**. A manifest that
    is perfectly signed — just by somebody else — would otherwise have its key
    trusted as this agent's. "The runner launched it" does not close that,
    because the launch and this fetch are separate channels: if the agent dies
    between reporting ready and being provisioned, whatever takes the port
    serves its own genuinely-signed manifest and passes verification.
    """
    other_port = 65000  # a port no agent in this run was launched on
    agent_http.overrides["/.well-known/aitp-manifest"] = lambda req: httpx.Response(
        200, text=_fake_agent_manifest(other_port)
    )
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False, cp=FakeCp(enabled=True),
        steps=[{"id": "prov", "type": "cp_provision_trust_anchor",
                "agent": "alice", "issuer_url": "https://issuer.test"}],
    )
    result = await _run(env)

    assert result.status == "failed", (
        "a manifest minted by a different agent was accepted; its key would "
        "have been pinned in the CP under alice's AID"
    )
    assert "refusing to pin a trust anchor" in (result.error or "")
    assert env.cp.pinned == [], "nothing may be pinned when identity does not match"


async def test_cp_provision_refuses_a_manifest_that_fails_signature_verification(agent_http) -> None:
    """The authenticity check this step actually performs — not just the AID pin.

    The AID-substitution test above is caught by the identity comparison
    regardless of whether `aitp.verify_manifest_json` runs at all, because a
    substituted manifest is self-consistently signed. This one tampers the
    manifest body *after* signing while leaving `aid` untouched, so the AID
    pin cannot be what catches it — only `aitp.verify_manifest_json` can.
    Before this test, deleting that call left `tests/unit` green.
    """
    def _tampered_manifest(req: httpx.Request) -> httpx.Response:
        # Derive from the same cached-per-port fixture the fake supervisor
        # uses for `ra.aid` (see FakeSupervisor.launch above), so the `aid`
        # here always agrees with the AID pin regardless of which port the
        # allocator happened to give "alice" — only the tamper is new.
        genuine = json.loads(_fake_agent_manifest(req.url.port))
        genuine["manifest"]["display_name"] = "tampered-after-signing"
        return httpx.Response(200, text=json.dumps(genuine))

    agent_http.overrides["/.well-known/aitp-manifest"] = _tampered_manifest

    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False, cp=FakeCp(enabled=True),
        steps=[{"id": "prov", "type": "cp_provision_trust_anchor",
                "agent": "alice", "issuer_url": "https://issuer.test"}],
    )
    result = await _run(env)

    assert result.status == "failed"
    assert "failed verification (signature_invalid)" in (result.error or "")
    assert env.cp.pinned == [], "nothing may be pinned from an unverified manifest"
    failures = [e for e in result.events if e.type == "manifest.verify_failed"]
    assert len(failures) == 1
    assert failures[0].cause == "signature_invalid"
    assert failures[0].source_url == env.supervisor.agents["alice"].manifest_url


async def test_cp_delegation_tree_skips_without_cp(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "tree", "type": "cp_delegation_tree", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["tree"] == {"delegations": [], "skipped": "no cp"}


async def test_cp_delegation_tree_flushes_events_before_reading(agent_http) -> None:
    cp = FakeCp(enabled=True, delegations=[{"jti": "d-1"}])
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False, cp=cp,
        steps=[{"id": "tree", "type": "cp_delegation_tree", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["tree"]
    # Derived from the fixture, not hard-coded: the fake supervisor now issues
    # the AID the served manifest actually declares.
    assert out["delegator"] == json.loads(_fake_agent_manifest(env.supervisor.agents["alice"].port))["manifest"]["aid"]
    assert out["count"] == 1
    assert out["delegations"] == [{"jti": "d-1"}]
    # Mid-run flush + post-run batch → two ingests, flush first and before
    # the projection read.
    assert len(cp.ingested) == 2
    flushed_types = {e.get("type") for e in cp.ingested[0]}
    assert "run.started" in flushed_types
    assert "cp.delegation.tree" in _event_types(result)


# --------------------------------------------------------------------------- #
# renewal / cache stats / session bundle / rotation
# --------------------------------------------------------------------------- #


async def test_renew_tct_swaps_held_token(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]},
        steps=[{"id": "renew", "type": "renew_tct", "agent": "alice", "via_peer": "bob"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["renew"]["jti"] == "jti-renewed"
    body = agent_http.body_of("/admin/renew-tct")
    assert body == {"peer_port": 9501}  # holder presents to the issuer (bob)
    renewed = [e for e in result.events if e.type == "tct.renewed"]
    assert renewed and renewed[0].jti == "jti-renewed"


async def test_renew_tct_requires_holder_and_issuer(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, eager=False,
        steps=[{"id": "renew", "type": "renew_tct", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "renew_tct requires agent (holder) and via_peer (issuer)" in result.error


async def test_tct_cache_stats_reads_agent_counters(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "stats", "type": "tct_cache_stats", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["stats"] == {"hits": 5, "misses": 1, "entries": 1}
    assert "tct.cache.stats" in _event_types(result)


async def test_session_bundle_export_then_verify(agent_http) -> None:
    env = make_env(
        agents={"coord": ["cap.a"], "member": ["cap.b"]},
        steps=[
            {"id": "export", "type": "export_session_bundle",
             "coordinator": "coord", "participants": ["member"]},
            {"id": "verify", "type": "verify_session_bundle",
             "verifier": "member", "via_step": "export"},
        ],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    # The coordinator signs the participants' held (coordinator-issued) TCTs.
    export_body = agent_http.body_of("/admin/export-session-bundle")
    assert export_body["participant_tcts"] == [
        {"aid": "aid-port-9501", "tct_token": "tok-9501"},
    ]
    assert result.outputs["export"]["session_id"] == "sess-1"
    # The verifier re-presents the exported envelope.
    verify_body = agent_http.body_of("/admin/verify-session-bundle")
    assert verify_body == {"bundle_envelope": {"payload": "bundle"}}
    assert result.outputs["verify"]["kind"] == "session_bundle"
    types = _event_types(result)
    assert "session.bundle.exported" in types
    assert "session.bundle.verified" in types


async def test_verify_session_bundle_requires_prior_envelope(agent_http) -> None:
    env = make_env(
        agents={"coord": ["cap.a"]}, eager=False,
        steps=[
            {"id": "note", "description": "no bundle here"},
            {"id": "verify", "type": "verify_session_bundle",
             "verifier": "coord", "via_step": "note"},
        ],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "did not produce a bundle_envelope" in result.error


async def test_rotate_keys_updates_the_running_agent_aid(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "rot", "type": "rotate_keys", "agent": "alice"}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["rot"] == {
        "old_aid": json.loads(_fake_agent_manifest(env.supervisor.agents["alice"].port))["manifest"]["aid"],
        "new_aid": "aid-rotated",  # from the fake /admin/rotate-keys endpoint
    }
    # The engine mutates its RunningAgent record so later steps see the new AID.
    assert env.supervisor.agents["alice"].aid == "aid-rotated"
    rotated = [e for e in result.events if e.type == "identity.key.rotated"]
    assert rotated and rotated[0].aid == "aid-rotated"


# --------------------------------------------------------------------------- #
# spki_pin_check (real SDK)
# --------------------------------------------------------------------------- #


async def test_spki_pin_check_matching_pin(agent_http) -> None:
    env = make_env(
        agents={}, eager=False,
        steps=[{"id": "pin", "type": "spki_pin_check",
                "cert_der_b64": _SPKI_CERT_B64,
                "pins": [_SPKI_MATCHING_PIN], "expect_status": 1}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["pin"]
    assert out["is_pinned"] is True
    assert out["pin_count"] == 1
    # The SDK-computed SPKI hash equals the known-good pin.
    assert out["computed_hash_b64"] == _SPKI_MATCHING_PIN
    assert "spki.pin.checked" in _event_types(result)


async def test_spki_pin_check_rejects_wrong_pin(agent_http) -> None:
    env = make_env(
        agents={}, eager=False,
        steps=[{"id": "pin", "type": "spki_pin_check",
                "cert_der_b64": _SPKI_CERT_B64,
                "pins": [_SPKI_WRONG_PIN], "expect_status": 0}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    assert result.outputs["pin"]["is_pinned"] is False


async def test_spki_pin_check_expectation_mismatch_fails_run(agent_http) -> None:
    env = make_env(
        agents={}, eager=False,
        steps=[{"id": "pin", "type": "spki_pin_check",
                "cert_der_b64": _SPKI_CERT_B64,
                "pins": [_SPKI_WRONG_PIN], "expect_status": 1}],
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "expected_pinned=True but got False" in result.error
    assert "step.unexpected_status" in _event_types(result)


# --------------------------------------------------------------------------- #
# fault injection
# --------------------------------------------------------------------------- #


async def test_fault_manifest_404_on_handshake_records_error_and_continues(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, eager=False,
        steps=[
            {"id": "hs", "type": "handshake", "initiator": "alice", "responder": "bob",
             "fault": {"kind": "manifest_404", "note": "demo"}},
            {"id": "note", "description": "run continues after the fault"},
        ],
    )
    result = await _run(env)
    assert result.status == "success", result.error  # fault never raises out
    out = result.outputs["hs"]
    assert out["fault_injected"] is True
    assert out["kind"] == "manifest_404"
    assert out["target"] == "bob"
    assert out["mutated_url"].endswith("-DOES-NOT-EXIST")
    assert out["error"] is not None  # the 502 handshake surfaced as an error
    types = _event_types(result)
    assert "step.fault_injected" in types
    assert "step.fault_complete" in types


async def test_fault_peer_offline_on_probe_reroutes_to_unbound_port(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["write.content"]},
        steps=[{"id": "probe", "type": "capability_probe",
                "agent": "alice", "target_agent": "bob", "capability": "write.content",
                "fault": {"kind": "peer_offline"}}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["probe"]
    assert out["kind"] == "peer_offline"
    assert ":1/" in out["mutated_url"]
    # The probe posted through the caller's /admin/invoke with peer_port=1.
    invoke_body = agent_http.body_of("/admin/invoke")
    assert invoke_body["peer_port"] == 1


async def test_fault_on_unsupported_step_type_is_recorded_not_raised(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"], "carol": ["cap.c"]},
        steps=[{"id": "del", "type": "delegate", "delegator": "alice",
                "delegatee": "carol", "via_peer": "bob", "scope": ["cap.b"],
                "fault": {"kind": "peer_offline"}}],
    )
    result = await _run(env)
    assert result.status == "success", result.error
    out = result.outputs["del"]
    assert out["fault_injected"] is True
    assert "not supported" in out["error"]


async def test_fault_with_unresolvable_target_fails_run(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, eager=False,
        steps=[{"id": "hs", "type": "handshake", "initiator": "alice",
                "fault": {"kind": "manifest_404"}}],  # no responder to target
    )
    result = await _run(env)
    assert result.status == "failed"
    assert "could not resolve target peer" in result.error


async def test_unknown_fault_kind_fails_run(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"], "bob": ["cap.b"]}, eager=False,
        steps=[{"id": "hs", "type": "handshake", "initiator": "alice", "responder": "bob",
                "fault": {"kind": "manifest_404"}}],
    )
    # The registry validates FaultKind, so an unknown kind can only appear
    # through a bug — bypass validation to prove the engine's exhaustive guard.
    env.scenario.spec.workflow.steps[0].fault = StepFault.model_construct(kind="chaos")
    result = await _run(env)
    assert result.status == "failed"
    assert "unknown fault kind 'chaos'" in result.error


# --- Phase 6: a cancelled run stays cancelled ---


def test_finalize_failure_preserves_an_already_cancelled_record() -> None:
    """`_finalize_failure` must not clobber a cancellation with `failed`.

    `/runs/{id}/cancel` upserts `status="cancelled"` before killing the
    agent subprocesses; that kill is what turns the background task's next
    inter-agent call into the exception `_finalize_failure` handles. Pinned
    at the unit tier (deterministic, store pre-seeded directly) rather than
    relying only on the timing-sensitive `AITP_E2E` integration test, which
    has to actually race a real subprocess kill to exercise this at all.
    """
    from aitp_playground.runner.context import RunContext

    env = make_env(agents={"alice": ["cap.a"]}, eager=False, steps=[])
    env.store.upsert("run-1", {"run_id": "run-1", "status": "cancelled"})
    ctx = RunContext(run_id="run-1", scenario_ref=SCENARIO_REF, store=env.store)

    result = env.runner._finalize_failure("run-1", "connection refused", ctx)

    assert result.status == "failed", "the RunResult itself reports what happened"
    record = env.store.get("run-1")
    assert record["status"] == "cancelled", (
        "the STORE record must stay cancelled — the caller already saw that "
        "outcome via /runs/{id}/cancel's response"
    )


def test_finalize_failure_upserts_failed_normally_when_not_cancelled() -> None:
    """The guard must not swallow every failure — only ones already cancelled."""
    from aitp_playground.runner.context import RunContext

    env = make_env(agents={"alice": ["cap.a"]}, eager=False, steps=[])
    env.store.upsert("run-1", {"run_id": "run-1", "status": "running"})
    ctx = RunContext(run_id="run-1", scenario_ref=SCENARIO_REF, store=env.store)

    env.runner._finalize_failure("run-1", "boom", ctx)

    assert env.store.get("run-1")["status"] == "failed"


async def test_run_failed_not_emitted_when_dispatch_races_a_cancel(agent_http) -> None:
    """The dispatch-level `run.failed`-emit guard (`engine.py:228-230`) must
    not fire for a run the caller already cancelled.

    Mirrors the real race `DECISIONS.md` D-16 describes: `/runs/{id}/cancel`
    upserts `status="cancelled"` and kills the agent subprocesses; that kill
    is what turns the in-flight step dispatch's next HTTP call into the
    exception `run()`'s `except Exception` handler catches. This guard is a
    distinct, sibling site to `_finalize_failure`'s store guard (already
    covered by `test_finalize_failure_preserves_an_already_cancelled_record`)
    and previously had no unit-tier coverage at all — only a flaky,
    timing-sensitive real-subprocess integration test. `run()` unconditionally
    upserts `status="running"` before dispatch, so the store can't be
    pre-seeded as cancelled; instead this flips it to "cancelled" as a side
    effect of the dispatch call itself, then raises, so the guard sees
    "cancelled" at the exact moment it checks.
    """
    env = make_env(
        agents={"alice": ["cap.a"]}, eager=False,
        steps=[{"id": "s1", "agent": "alice", "capability": "cap.a"}],
    )

    def _cancel_then_blow_up(request: httpx.Request) -> httpx.Response:
        # The moment the step's self-execute call reaches the (fake) agent,
        # simulate a concurrent /runs/{id}/cancel: flip the store first, then
        # raise — standing in for the subprocess kill that turns this call
        # into an exception in the real race.
        env.store.upsert("run-1", {"status": "cancelled"})
        raise RuntimeError("connection reset by peer")

    agent_http.overrides["/admin/self-execute"] = _cancel_then_blow_up

    result = await _run(env)

    assert result.status == "failed", result.error
    assert "run.failed" not in _event_types(result), (
        "the dispatch-level guard must not emit run.failed for a run the "
        "caller already saw as cancelled"
    )
    record = env.store.get("run-1")
    assert record["status"] == "cancelled", (
        "the STORE record must stay cancelled, not be overwritten to "
        "'failed' downstream of the guard"
    )
