"""#117 Phase 1: derived SDK feature requirements and the engine preflight.

Every feature exists on aitp-sdk 0.23, so absence is simulated: the engine
through its ``capabilities_provider`` seam, the API and CLI by replacing the
probe (``capabilities._probe``) behind the cached ``get_capabilities``.
"""
from __future__ import annotations

import shutil
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from aitp_playground import capabilities as caps
from aitp_playground import cli
from aitp_playground.config import Settings
from aitp_playground.errors import SdkFeatureUnavailableError
from aitp_playground.observability.metrics import metrics
from aitp_playground.registry.service import RegistryService
from aitp_playground.registry.templates import apply_template
from aitp_playground.runner import engine as engine_mod
from aitp_playground.runner.engine import ScenarioRunner
from aitp_playground.runner.store import RunStore
from tests.unit import test_engine_run as engine_tests
from tests.unit.test_engine_run import (
    FakeAdapters,
    FakeCp,
    FakeSupervisor,
    _event_types,
    _run,
    make_env,
)

ROOT = Path(__file__).resolve().parents[2]
RENEWAL_REF = "intra-org/tct-renewal@1.0.0"
BUNDLE_REF = "intra-org/session-bundle@1.0.0"
STABLE_REF = "intra-org/research-and-write@1.0.0"

RENEW_STEP = {"id": "renew", "type": "renew_tct", "agent": "alice", "via_peer": "bob"}
CAP_STEP = {"id": "call", "agent": "alice", "capability": "cap.b", "input_template": "x"}


def _caps(*absent: str, sdk_available: bool = True) -> dict[str, Any]:
    return {
        "sdk_available": sdk_available,
        "version": "0.23.0" if sdk_available else None,
        "features": {f: sdk_available and f not in absent for f in caps.ALL_FEATURES},
    }


def _provider(*absent: str, sdk_available: bool = True):
    return lambda: _caps(*absent, sdk_available=sdk_available)


@pytest.fixture(autouse=True)
def _fresh_probe_and_metrics():
    caps.get_capabilities.cache_clear()
    metrics.reset()
    yield
    caps.get_capabilities.cache_clear()


@pytest.fixture()
def agent_http(monkeypatch) -> "engine_tests._AgentHttpHandler":
    """test_engine_run's fake agent HTTP layer (a fixture can't be imported
    under its own name without shadowing the test parameter)."""
    handler = engine_tests._AgentHttpHandler()
    transport = httpx.MockTransport(handler)

    def _client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, auth=kwargs.get("auth"))

    monkeypatch.setattr(engine_mod, "httpx", types.SimpleNamespace(AsyncClient=_client))
    return handler


@pytest.fixture()
def simulate_absent(monkeypatch):
    """Make the process-wide probe report the given features absent."""
    def _apply(*absent: str, sdk_available: bool = True) -> None:
        monkeypatch.setattr(caps, "_probe", _provider(*absent, sdk_available=sdk_available))
        caps.get_capabilities.cache_clear()
    return _apply


def _registry(scenarios_dir: Path = ROOT / "scenarios") -> RegistryService:
    return RegistryService(Settings(scenarios_dir=str(scenarios_dir)))


def _counter(name: str, **labels: str) -> float:
    key = (name, tuple(sorted(labels.items())))
    return metrics.snapshot()["counters"].get(key, 0.0)


def _gauge(name: str) -> float:
    return metrics.snapshot()["gauges"].get((name, ()), 0.0)


# ── derivation ──────────────────────────────────────────────────────────────


def test_step_table_values_are_known_features() -> None:
    assert set(caps.STEP_SDK_FEATURES.values()) <= set(caps.ALL_FEATURES)
    assert dict(caps.STEP_SDK_FEATURES) == {
        "renew_tct": caps.FEATURE_TCT_RENEWAL,
        "export_session_bundle": caps.FEATURE_SESSION_BUNDLE,
        "verify_session_bundle": caps.FEATURE_SESSION_BUNDLE,
    }


def test_requirements_across_the_whole_registry() -> None:
    """Only the two gated scenarios need anything; every other shipped
    scenario and template runs on the stable surface."""
    reg = _registry()
    expected = {RENEWAL_REF: ["tct_renewal"], BUNDLE_REF: ["session_bundle"]}
    seen = set()
    for sv in reg.list_scenarios():
        ref = f"{sv.metadata.pack}/{sv.metadata.scenario}@{sv.metadata.version}"
        seen.add(ref)
        assert caps.required_sdk_features(sv) == expected.get(ref, []), ref
        for tpl in reg.list_templates(ref):
            resolved = apply_template(sv, tpl)
            assert caps.required_sdk_features(resolved) == expected.get(ref, []), (
                f"{ref} template {tpl.metadata.name}"
            )
    assert set(expected) <= seen


def test_requirements_are_sorted_and_deduplicated() -> None:
    sv = _registry().get_scenario(BUNDLE_REF)
    # export + verify both map to session_bundle: listed once.
    assert [s.type for s in sv.spec.workflow.steps].count("verify_session_bundle") >= 1
    assert caps.required_sdk_features(sv) == ["session_bundle"]


def test_missing_features_with_and_without_sdk() -> None:
    sv = _registry().get_scenario(RENEWAL_REF)
    assert caps.missing_sdk_features(sv, _provider()) == []
    assert caps.missing_sdk_features(sv, _provider("tct_renewal")) == ["tct_renewal"]
    assert caps.missing_sdk_features(sv, _provider("session_bundle")) == []
    # No wheel at all: every requirement is missing.
    assert caps.missing_sdk_features(sv, _provider(sdk_available=False)) == ["tct_renewal"]
    stable = _registry().get_scenario(STABLE_REF)
    assert caps.missing_sdk_features(stable, _provider(sdk_available=False)) == []


def test_missing_features_defaults_to_the_installed_probe(simulate_absent) -> None:
    sv = _registry().get_scenario(RENEWAL_REF)
    simulate_absent()  # everything present
    assert caps.missing_sdk_features(sv) == []
    simulate_absent("tct_renewal")
    assert caps.missing_sdk_features(sv) == ["tct_renewal"]


@pytest.mark.parametrize("present", [
    ("build_renewal_request",),
    ("process_renewal_request",),
    ("build_renewal_request", "process_renewal_request"),
])
def test_renewal_probe_needs_both_methods(monkeypatch, present) -> None:
    import sys

    agent = type("AitpAgent", (), {name: staticmethod(lambda: None) for name in present})
    monkeypatch.setitem(sys.modules, "aitp", types.SimpleNamespace(AitpAgent=agent))
    caps.get_capabilities.cache_clear()
    assert caps.has_feature(caps.FEATURE_TCT_RENEWAL) is (len(present) == 2)


def test_error_message_and_attributes() -> None:
    err = SdkFeatureUnavailableError(RENEWAL_REF, ["tct_renewal"], "0.23.0")
    assert err.code == "sdk_feature_unavailable"
    assert err.status_code == 409
    assert err.missing == ["tct_renewal"]
    assert str(err) == (
        f"sdk_feature_unavailable: {RENEWAL_REF} needs tct_renewal; "
        "the installed aitp-sdk 0.23.0 lacks it"
    )
    assert "aitp-sdk (none)" in str(SdkFeatureUnavailableError("x", ["a", "b"], None))


# ── engine preflight ────────────────────────────────────────────────────────


def _real_runner(capabilities_provider) -> tuple[ScenarioRunner, FakeSupervisor]:
    settings = Settings(cp_base_url="", cp_api_key="")
    supervisor = FakeSupervisor()
    from aitp_playground.hosting.bootstrap import BootstrapBuilder
    from aitp_playground.hosting.port_allocator import PortAllocator
    from aitp_playground.trust.orchestrator import TrustOrchestrator

    cp = FakeCp()
    runner = ScenarioRunner(
        registry=_registry(),
        supervisor=supervisor,
        bootstrap_builder=BootstrapBuilder(settings),
        adapters=FakeAdapters(),
        trust=TrustOrchestrator(cp, settings),
        cp=cp,
        port_alloc=PortAllocator(start=9600),
        config=settings,
        store=RunStore(),
        capabilities_provider=capabilities_provider,
    )
    return runner, supervisor


async def test_shipped_gated_scenario_is_refused_before_spawn() -> None:
    runner, supervisor = _real_runner(_provider("tct_renewal"))
    result = await runner.run(scenario_ref=RENEWAL_REF, inputs={"topic": "x"}, run_id="r-ref")
    assert result.status == "failed"
    assert _event_types(result) == ["run.failed"]
    failed = result.events[0]
    assert failed.code == "sdk_feature_unavailable"
    assert "tct_renewal" in failed.error
    assert result.error == failed.error
    assert supervisor.launched == []
    record = runner.store.get("r-ref")
    assert record["status"] == "failed"
    assert record["events"][-1]["code"] == "sdk_feature_unavailable"


async def test_absence_does_not_affect_a_stable_scenario(agent_http) -> None:
    """research-and-write needs nothing optional: with every optional
    feature absent it still passes the preflight and its agents launch."""
    sv = _registry().get_scenario(STABLE_REF)
    assert caps.missing_sdk_features(sv, _provider(*caps.ALL_FEATURES)) == []
    runner, supervisor = _real_runner(_provider(*caps.ALL_FEATURES))
    result = await runner.run(scenario_ref=STABLE_REF, inputs={"topic": "x"}, run_id="r-ok")
    assert "run.started" in _event_types(result)
    assert all(e.code != "sdk_feature_unavailable" for e in result.events)
    assert supervisor.launched == [a.id for a in sv.spec.agents]


async def test_engine_refusal_with_spawn_spy(agent_http) -> None:
    env = make_env(agents={"alice": ["cap.a"], "bob": ["cap.b"]}, steps=[RENEW_STEP])
    env.runner.capabilities_provider = _provider("tct_renewal")
    result = await _run(env)
    assert result.status == "failed"
    types_ = _event_types(result)
    assert types_ == ["run.failed"]
    assert not any(t.startswith(("agent.", "trust.")) for t in types_)
    assert result.events[0].code == "sdk_feature_unavailable"
    assert env.supervisor.launched == []
    assert agent_http.paths() == []


async def test_stable_steps_run_with_features_absent(agent_http) -> None:
    env = make_env(agents={"alice": ["cap.a"], "bob": ["cap.b"]}, steps=[CAP_STEP])
    env.runner.capabilities_provider = _provider(*caps.ALL_FEATURES)
    result = await _run(env)
    assert result.status == "success", result.error
    assert env.supervisor.launched == ["alice", "bob"]


async def test_no_sdk_at_all_names_every_missing_feature(agent_http) -> None:
    steps = [
        RENEW_STEP,
        {"id": "export", "type": "export_session_bundle", "agent": "alice"},
    ]
    env = make_env(agents={"alice": ["cap.a"], "bob": ["cap.b"]}, steps=steps)
    env.runner.capabilities_provider = _provider(sdk_available=False)
    result = await _run(env)
    assert result.events[0].code == "sdk_feature_unavailable"
    assert "session_bundle, tct_renewal" in result.error
    assert "aitp-sdk (none)" in result.error
    assert env.supervisor.launched == []


async def test_template_adding_a_gated_step_is_refused(tmp_path) -> None:
    """Requirements come from the resolved scenario: a template that adds
    renew_tct to a stable scenario makes it gated."""
    scenarios = tmp_path / "scenarios"
    shutil.copytree(ROOT / "scenarios", scenarios)
    _write_renewing_template(scenarios)
    runner, supervisor = _real_runner(_provider("tct_renewal"))
    runner.registry = _registry(scenarios)

    plain = runner.registry.get_scenario_resolved(STABLE_REF)
    assert caps.required_sdk_features(plain) == []
    result = await runner.run(
        scenario_ref=STABLE_REF, inputs={"topic": "x"}, template="renewing", run_id="r-tpl",
    )
    assert result.status == "failed"
    assert result.events[0].code == "sdk_feature_unavailable"
    assert supervisor.launched == []


def _write_renewing_template(scenarios: Path) -> None:
    (scenarios / "intra-org/research-and-write/1.0.0/templates/renewing.yaml").write_text(
        """apiVersion: agentidentitytrustprotocol.io/playground/v1
kind: ScenarioTemplate
metadata:
  name: renewing
  summary: research-and-write plus an in-band TCT renewal.
spec:
  workflow:
    steps:
      - id: trust
        description: All agents perform AITP mutual handshake.
      - id: renew
        type: renew_tct
        agent: researcher
        via_peer: writer
"""
    )


async def test_unknown_scenario_and_bad_inputs_carry_codes(agent_http) -> None:
    env = make_env(agents={"alice": ["cap.a"]}, steps=[])
    missing = await _run(env, ref="nope/missing@1.0.0", run_id="r-missing")
    assert missing.events[0].code == "scenario_not_found"
    bad = await _run(env, inputs={}, run_id="r-bad")
    assert bad.events[-1].type == "run.failed"
    assert bad.events[-1].code == "inputs_invalid"


async def test_failure_in_port_and_bootstrap_block_is_terminal(agent_http) -> None:
    """engine.py's ports/manifest/bootstrap block now sits inside the run
    try: a raise there emits run.failed, frees the ports, and leaves no
    runs_active leak."""
    env = make_env(agents={"alice": ["cap.a"], "bob": ["cap.b"]}, steps=[CAP_STEP])

    def boom(ref: str):
        raise RuntimeError("manifest store unavailable")

    env.runner.registry.get_agent_manifest = boom
    result = await _run(env)
    assert result.status == "failed"
    assert _event_types(result) == ["run.started", "run.failed"]
    assert "manifest store unavailable" in result.error
    assert result.events[-1].code is None  # not a coded PlaygroundError
    assert env.supervisor.launched == []
    assert {env.ports.allocate(), env.ports.allocate()} == {9500, 9501}
    assert _gauge("aitp_playground_runs_active") == 0.0
    assert _counter("aitp_playground_runs_refused_total", code="unknown") == 0.0


async def test_a_foreign_code_attribute_is_not_copied(agent_http) -> None:
    """Only PlaygroundError codes are run codes. An exception with a
    non-str ``code`` (urllib's HTTPError has an int) must still end the run
    with run.failed instead of failing RunEvent validation mid-except."""
    env = make_env(agents={"alice": ["cap.a"]}, steps=[])

    class Foreign(Exception):
        code = 503

    def boom(ref: str):
        raise Foreign("upstream said 503")

    env.runner.registry.get_agent_manifest = boom
    result = await _run(env)
    assert result.status == "failed"
    assert result.events[-1].type == "run.failed"
    assert result.events[-1].code is None
    assert env.store.get("run-1")["status"] == "failed"
    assert _gauge("aitp_playground_runs_active") == 0.0


async def test_coded_crash_after_start_keeps_its_code(agent_http) -> None:
    env = make_env(
        agents={"alice": ["cap.a"]}, steps=[],
        adapters=FakeAdapters(errors=["missing entrypoint"]),
    )
    result = await _run(env)
    assert result.events[-1].type == "run.failed"
    assert result.events[-1].code == "internal_error"


# ── metrics ─────────────────────────────────────────────────────────────────


async def test_refusals_are_counted_and_leave_the_gauge_alone(agent_http) -> None:
    env = make_env(agents={"alice": ["cap.a"], "bob": ["cap.b"]}, steps=[RENEW_STEP])
    env.runner.capabilities_provider = _provider("tct_renewal")
    await _run(env, run_id="r1")
    await _run(env, ref="nope/missing@1.0.0", run_id="r2")
    assert _counter("aitp_playground_runs_refused_total", code="sdk_feature_unavailable") == 1.0
    assert _counter("aitp_playground_runs_refused_total", code="scenario_not_found") == 1.0
    assert _counter("aitp_playground_runs_total", status="failed") == 2.0
    assert _gauge("aitp_playground_runs_active") == 0.0


# ── API ─────────────────────────────────────────────────────────────────────


def _client(monkeypatch, scenarios: Path = ROOT / "scenarios") -> TestClient:
    monkeypatch.setenv("SCENARIOS_DIR", str(scenarios))
    from aitp_playground import config
    from aitp_playground.main import create_app

    monkeypatch.setattr(config, "_settings", None)
    return TestClient(create_app())


def _listed(client: TestClient, ref: str) -> dict:
    body = client.get("/scenarios").json()
    return next(s for s in body["scenarios"] if s["ref"] == ref)


@pytest.mark.parametrize("absent", [False, True])
def test_api_reports_requirements_on_every_route(monkeypatch, tmp_path, simulate_absent, absent) -> None:
    scenarios = tmp_path / "scenarios"
    shutil.copytree(ROOT / "scenarios", scenarios)
    _write_renewing_template(scenarios)
    simulate_absent(*(("tct_renewal", "session_bundle") if absent else ()))
    client = _client(monkeypatch, scenarios)
    available = not absent

    bundle = _listed(client, BUNDLE_REF)
    assert bundle["sdk_features_required"] == ["session_bundle"]
    assert bundle["sdk_available"] is available
    stable = _listed(client, STABLE_REF)
    assert stable["sdk_features_required"] == []
    assert stable["sdk_available"] is True

    detail = client.get(f"/scenarios/{BUNDLE_REF}").json()
    assert detail["sdk_features_required"] == ["session_bundle"]
    assert detail["sdk_available"] is available

    path = "/scenarios/intra-org/research-and-write@1.0.0"
    detail = client.get(path).json()
    assert detail["sdk_features_required"] == []
    by_name = {t["name"]: t for t in detail["templates"]}
    assert by_name["renewing"]["sdk_features_required"] == ["tct_renewal"]
    assert by_name["renewing"]["sdk_available"] is available
    assert by_name["revoking"]["sdk_features_required"] == []
    assert by_name["revoking"]["sdk_available"] is True

    listed = {t["name"]: t for t in client.get(f"{path}/templates").json()["templates"]}
    assert listed["renewing"] == by_name["renewing"]
    assert listed["revoking"] == by_name["revoking"]

    one = client.get(f"{path}/templates/renewing").json()
    assert one["sdk_features_required"] == ["tct_renewal"]
    assert one["sdk_available"] is available


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli(monkeypatch, capsys, *argv: str, scenarios: Path = ROOT / "scenarios") -> tuple[int, str, str]:
    monkeypatch.setenv("SCENARIOS_DIR", str(scenarios))
    from aitp_playground import config

    monkeypatch.setattr(config, "_settings", None)
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_dry_run_exit_codes(monkeypatch, capsys, simulate_absent) -> None:
    args = ("dry-run", RENEWAL_REF, "--inputs", '{"topic":"x"}')
    simulate_absent()
    rc, out, _ = _cli(monkeypatch, capsys, *args)
    assert rc == 0
    assert "sdk features: tct_renewal (available)" in out

    simulate_absent("tct_renewal")
    rc, out, err = _cli(monkeypatch, capsys, *args)
    assert rc == 1
    assert "tct_renewal (MISSING in aitp-sdk 0.23.0)" in out
    assert "lacks a feature" in err

    rc, out, _ = _cli(monkeypatch, capsys, "dry-run", STABLE_REF, "--inputs", '{"topic":"x"}')
    assert rc == 0
    assert "sdk features" not in out


def test_cli_validate_warns_but_passes(monkeypatch, capsys, simulate_absent) -> None:
    simulate_absent("tct_renewal")
    rc, out, _ = _cli(monkeypatch, capsys, "validate", str(ROOT / "scenarios/intra-org/tct-renewal"))
    assert rc == 0
    assert f"ok  {RENEWAL_REF}" in out
    assert "tct_renewal (MISSING in aitp-sdk 0.23.0)" in out


def test_cli_template_requirements(monkeypatch, capsys, tmp_path, simulate_absent) -> None:
    """validate flags a template that adds a gated step; dry-run --template
    refuses it on a wheel without the feature."""
    scenarios = tmp_path / "scenarios"
    shutil.copytree(ROOT / "scenarios", scenarios)
    _write_renewing_template(scenarios)
    simulate_absent("tct_renewal")

    rc, out, _ = _cli(
        monkeypatch, capsys, "validate", str(scenarios / "intra-org/research-and-write"),
        scenarios=scenarios,
    )
    assert rc == 0
    after = out.split("ok    template renewing", 1)[1].splitlines()
    assert "tct_renewal (MISSING in aitp-sdk 0.23.0)" in after[1]
    assert "sdk features" not in out.split("ok    template renewing", 1)[0]

    base = ("dry-run", STABLE_REF, "--inputs", '{"topic":"x"}')
    rc, _, _ = _cli(monkeypatch, capsys, *base, scenarios=scenarios)
    assert rc == 0
    rc, out, _ = _cli(monkeypatch, capsys, *base, "--template", "renewing", scenarios=scenarios)
    assert rc == 1
    assert "tct_renewal (MISSING" in out


@pytest.mark.parametrize("present", [
    ("SessionBundleBuilder",),
    ("verify_session_bundle",),
    ("SessionBundleBuilder", "verify_session_bundle"),
])
def test_session_bundle_probe_needs_both_names(monkeypatch, present) -> None:
    import sys

    fake = types.SimpleNamespace(**{name: object() for name in present})
    monkeypatch.setitem(sys.modules, "aitp", fake)
    caps.get_capabilities.cache_clear()
    assert caps.has_feature(caps.FEATURE_SESSION_BUNDLE) is (len(present) == 2)


def test_post_runs_end_to_end_refusal(monkeypatch, simulate_absent) -> None:
    """The API → engine → metrics seam: POST /runs still answers 202, the
    background run ends failed with the coded refusal and nothing spawned,
    and /metrics counts it as a refusal."""
    from tests._auth import AUTH

    simulate_absent("tct_renewal")
    client = _client(monkeypatch)
    client.headers.update(AUTH)
    posted = client.post("/runs", json={"scenario_ref": RENEWAL_REF, "inputs": {"topic": "x"}})
    assert posted.status_code == 202, posted.text
    rid = posted.json()["run_id"]

    run = client.get(f"/runs/{rid}").json()
    assert run["status"] == "failed"
    assert run["error"].startswith(f"sdk_feature_unavailable: {RENEWAL_REF} needs tct_renewal")
    types_ = [e["type"] for e in run["events"]]
    assert types_ == ["run.failed"]
    assert run["events"][0]["code"] == "sdk_feature_unavailable"

    text = client.get("/metrics").text
    assert 'aitp_playground_runs_refused_total{code="sdk_feature_unavailable"} 1' in text
