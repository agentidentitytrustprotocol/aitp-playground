"""Unit tests for the pure/branching helpers in ScenarioRunner.

These exercise the engine's routing and templating logic without spawning agent
subprocesses, hitting the network, or calling an LLM — the parts that are only
ever touched by the (env-gated, slow) integration e2e tests otherwise.

The helpers under test only read a handful of attributes off their arguments,
so we build the runner with ``object.__new__`` (skipping the 9-dependency
constructor) and feed lightweight stand-in objects.
"""
from __future__ import annotations

from types import SimpleNamespace


from aitp_playground.runner.context import RunContext, RunEvent
from aitp_playground.runner.engine import ScenarioRunner


def _runner() -> ScenarioRunner:
    """A ScenarioRunner with no wired dependencies — fine for helpers that
    don't touch ``self.*`` (or whose ``self.*`` calls we patch)."""
    return object.__new__(ScenarioRunner)


def _scenario(*agent_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(agents=[SimpleNamespace(id=a) for a in agent_ids])
    )


def _manifest(*caps: str) -> SimpleNamespace:
    return SimpleNamespace(spec=SimpleNamespace(aitp=SimpleNamespace(offered_caps=list(caps))))


# --------------------------------------------------------------------------- #
# _find_capability_holder
# --------------------------------------------------------------------------- #


def test_find_capability_holder_returns_sole_offerer() -> None:
    runner = _runner()
    scenario = _scenario("alice", "bob")
    manifests = {"alice": _manifest("read.data"), "bob": _manifest("write.data")}
    assert runner._find_capability_holder("write.data", scenario, manifests) == "bob"


def test_find_capability_holder_prefers_named_agent_when_it_offers_cap() -> None:
    """When two agents offer the same capability, an explicit ``prefer`` that
    also offers it keeps the step a self-execute instead of cross-routing."""
    runner = _runner()
    scenario = _scenario("researcher", "sub-researcher")
    manifests = {
        "researcher": _manifest("research.query"),
        "sub-researcher": _manifest("research.query"),
    }
    holder = runner._find_capability_holder(
        "research.query", scenario, manifests, prefer="sub-researcher"
    )
    assert holder == "sub-researcher"


def test_find_capability_holder_falls_back_when_preferred_lacks_cap() -> None:
    runner = _runner()
    scenario = _scenario("alice", "bob")
    manifests = {"alice": _manifest("read.data"), "bob": _manifest("write.data")}
    # alice is preferred but doesn't offer write.data → fall back to bob.
    holder = runner._find_capability_holder(
        "write.data", scenario, manifests, prefer="alice"
    )
    assert holder == "bob"


def test_find_capability_holder_returns_first_in_scenario_order() -> None:
    runner = _runner()
    scenario = _scenario("alice", "bob")
    manifests = {"alice": _manifest("shared.cap"), "bob": _manifest("shared.cap")}
    # No preference → first agent in scenario order wins.
    assert runner._find_capability_holder("shared.cap", scenario, manifests) == "alice"


def test_find_capability_holder_returns_none_when_unoffered() -> None:
    runner = _runner()
    scenario = _scenario("alice", "bob")
    manifests = {"alice": _manifest("read.data"), "bob": _manifest("write.data")}
    assert runner._find_capability_holder("delete.data", scenario, manifests) is None


def test_find_capability_holder_ignores_unknown_prefer() -> None:
    runner = _runner()
    scenario = _scenario("alice", "bob")
    manifests = {"alice": _manifest("read.data"), "bob": _manifest("read.data")}
    # prefer points at an agent with no manifest entry → ignored, fall through.
    holder = runner._find_capability_holder(
        "read.data", scenario, manifests, prefer="ghost"
    )
    assert holder == "alice"


# --------------------------------------------------------------------------- #
# _find_tct_jti
# --------------------------------------------------------------------------- #


def test_find_tct_jti_matches_audience_initiated_handshake() -> None:
    events = [
        RunEvent(type="trust.established", initiator="alice", target="bob", jti="a-to-b"),
        RunEvent(type="trust.established", initiator="bob", target="alice", jti="b-to-a"),
    ]
    # The TCT bob issued to alice is the one where alice initiated toward bob.
    assert ScenarioRunner._find_tct_jti(events, audience="alice", issuer="bob") == "a-to-b"


def test_find_tct_jti_returns_most_recent_match() -> None:
    events = [
        RunEvent(type="trust.established", initiator="alice", target="bob", jti="old"),
        RunEvent(type="trust.established", initiator="alice", target="bob", jti="new"),
    ]
    assert ScenarioRunner._find_tct_jti(events, audience="alice", issuer="bob") == "new"


def test_find_tct_jti_none_when_no_match() -> None:
    events = [
        RunEvent(type="trust.established", initiator="alice", target="bob", jti="a-to-b"),
    ]
    assert ScenarioRunner._find_tct_jti(events, audience="carol", issuer="bob") is None


def test_find_tct_jti_ignores_non_trust_events_and_missing_jti() -> None:
    events = [
        RunEvent(type="step.complete", initiator="alice", target="bob", jti="not-trust"),
        RunEvent(type="trust.established", initiator="alice", target="bob", jti=None),
    ]
    assert ScenarioRunner._find_tct_jti(events, audience="alice", issuer="bob") is None


# --------------------------------------------------------------------------- #
# _resolve_step_input
# --------------------------------------------------------------------------- #


def _step(*, input_from=None, input_template=None) -> SimpleNamespace:
    return SimpleNamespace(input_from=input_from, input_template=input_template)


def test_resolve_step_input_prefers_prior_step_output() -> None:
    runner = _runner()
    step = _step(input_from="research", input_template="ignored {{ inputs.topic }}")
    out = runner._resolve_step_input(
        step, inputs={"topic": "AI"}, step_outputs={"research": {"summary": 42}}
    )
    assert out == {"summary": 42}


def test_resolve_step_input_falls_through_when_input_from_missing() -> None:
    runner = _runner()
    step = _step(input_from="absent", input_template="topic is {{ inputs.topic }}")
    out = runner._resolve_step_input(
        step, inputs={"topic": "AI"}, step_outputs={}
    )
    # input_from doesn't resolve → template path is used.
    assert out == "topic is AI"


def test_resolve_step_input_renders_multiple_template_vars() -> None:
    runner = _runner()
    step = _step(input_template="q={{ inputs.q }} kind={{ inputs.kind }}")
    out = runner._resolve_step_input(
        step, inputs={"q": "what?", "kind": "essay"}, step_outputs={}
    )
    assert out == "q=what? kind=essay"


def test_resolve_step_input_stringifies_non_string_values() -> None:
    runner = _runner()
    step = _step(input_template="count={{ inputs.n }}")
    out = runner._resolve_step_input(step, inputs={"n": 7}, step_outputs={})
    assert out == "count=7"


def test_resolve_step_input_leaves_unmatched_placeholders() -> None:
    runner = _runner()
    step = _step(input_template="hi {{ inputs.missing }}")
    out = runner._resolve_step_input(step, inputs={"topic": "AI"}, step_outputs={})
    assert out == "hi {{ inputs.missing }}"


def test_resolve_step_input_returns_raw_inputs_without_template() -> None:
    runner = _runner()
    step = _step()
    inputs = {"topic": "AI", "depth": 3}
    assert runner._resolve_step_input(step, inputs=inputs, step_outputs={}) == inputs


# --------------------------------------------------------------------------- #
# _establish_pairwise_trust
# --------------------------------------------------------------------------- #


async def test_establish_pairwise_trust_runs_bidirectional_handshakes() -> None:
    """N agents → every ordered pair handshakes (N*(N-1) calls), each direction
    using the *target's* peer info."""
    runner = _runner()
    scenario = _scenario("a", "b", "c")
    running = {"a": "RA", "b": "RB", "c": "RC"}
    peers = {"a": {"manifest_url": "url-a"}, "b": {"manifest_url": "url-b"}, "c": {"manifest_url": "url-c"}}
    ctx = RunContext(run_id="r1", scenario_ref="x/y@1.0.0")

    calls: list[tuple] = []

    async def fake_ensure_trust(initiator, target, peer_info, grants, _ctx) -> None:
        calls.append((initiator, target, peer_info["manifest_url"], grants))

    runner._ensure_trust = fake_ensure_trust  # type: ignore[method-assign]

    await runner._establish_pairwise_trust(scenario, running, peers, ctx)

    # 3 agents → 3 unordered pairs → 6 directed handshakes.
    assert len(calls) == 6
    directed = {(c[0], c[1]) for c in calls}
    assert directed == {
        ("RA", "RB"), ("RB", "RA"),
        ("RA", "RC"), ("RC", "RA"),
        ("RB", "RC"), ("RC", "RB"),
    }
    # Each call resolves the *target's* manifest url and requests no grants.
    for initiator, target, url, grants in calls:
        expected = {"RA": "url-a", "RB": "url-b", "RC": "url-c"}[target]
        assert url == expected
        assert grants is None


async def test_establish_pairwise_trust_single_agent_is_noop() -> None:
    runner = _runner()
    scenario = _scenario("solo")
    ctx = RunContext(run_id="r1", scenario_ref="x/y@1.0.0")
    called = False

    async def fake_ensure_trust(*args, **kwargs) -> None:
        nonlocal called
        called = True

    runner._ensure_trust = fake_ensure_trust  # type: ignore[method-assign]
    await runner._establish_pairwise_trust(scenario, {"solo": "RS"}, {"solo": {}}, ctx)
    assert called is False
