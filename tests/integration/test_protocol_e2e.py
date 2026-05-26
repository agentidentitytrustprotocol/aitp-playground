"""End-to-end test: the 4 AITP-protocol scenarios that don't exist to make
the LLM go brrr, but to demonstrate identity/trust/delegation enforcement.

  - intra-org/trust-gate         capability call denied without TCT, then granted
  - intra-org/delegation-chain   single-hop delegation (RFC-AITP-0006)
  - intra-org/revocation-demo    TCT revocation (RFC-AITP-0008)
  - intra-org/scoped-capabilities grant intersection on handshake

These do not require OpenAI — the LLM path is incidental. Researcher/writer
agents fall back to their deterministic stubs and that's fine, the assertions
are on the AITP-protocol-level outcomes, not on the LLM output.

Gated on ``AITP_PROTOCOL_E2E=1``. Intended to run inside the ``tests``
container of ``docker-compose.test.yml`` next to test_llm_e2e.py.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AITP_PROTOCOL_E2E"),
    reason="Live protocol e2e — set AITP_PROTOCOL_E2E=1 to enable",
)

PLAYGROUND_URL = os.environ.get("PLAYGROUND_URL", "http://localhost:8000")
CP_URL = os.environ.get("CP_URL", "http://localhost:4000")
CP_API_KEY = os.environ.get("CP_API_KEY", "")

_TERMINAL = {"success", "failed", "cancelled"}
_RUN_DEADLINE_SECS = 180


@dataclass
class ProtocolCase:
    ref: str
    inputs: dict
    # Validator runs against the terminal run body — receives the whole dict
    # (status / outputs / events / error) and is expected to assert.
    check: Callable[[dict], None]


def _start_run(client: httpx.Client, case: ProtocolCase) -> str:
    r = client.post("/runs", json={"scenario_ref": case.ref, "inputs": case.inputs})
    assert r.status_code == 202, f"POST /runs failed: {r.status_code} {r.text}"
    return r.json()["run_id"]


def _wait_for_terminal(client: httpx.Client, run_id: str) -> dict:
    deadline = time.time() + _RUN_DEADLINE_SECS
    body: dict = {}
    while time.time() < deadline:
        r = client.get(f"/runs/{run_id}")
        assert r.status_code == 200, f"GET /runs/{run_id}: {r.status_code} {r.text}"
        body = r.json()
        if body.get("status") in _TERMINAL:
            return body
        time.sleep(0.5)
    pytest.fail(
        f"run {run_id} did not finish within {_RUN_DEADLINE_SECS}s; "
        f"last status={body.get('status')!r}"
    )


def _step(body: dict, step_id: str) -> Any:
    outputs = body.get("outputs") or {}
    assert step_id in outputs, (
        f"step {step_id!r} missing from outputs {list(outputs.keys())}"
    )
    return outputs[step_id]


# ── per-scenario validators ────────────────────────────────────────────────


def _check_trust_gate(body: dict) -> None:
    """probe_no_tct must observe a 403, then post-handshake write_with_trust
    must succeed (no error wrapper)."""
    probe = _step(body, "probe_no_tct")
    assert probe.get("status_code") == 403, (
        f"trust-gate: expected 403 from probe_no_tct, got {probe.get('status_code')!r}"
    )
    assert probe.get("rejected") is True and probe.get("matched") is True
    # establish_trust is a handshake — engine emits step.complete with {"trust":
    # "established"} as the recorded output.
    assert _step(body, "establish_trust") == {"trust": "established"}
    # write_with_trust returns the writer's actual capability output. If the
    # engine had hit a 4xx the workflow step would have raised and the run
    # status would be 'failed' — but assert the output is a non-error shape
    # too, defensively.
    write_result = _step(body, "write_with_trust")
    if isinstance(write_result, dict):
        assert not write_result.get("error"), (
            f"trust-gate: write_with_trust looks like an error wrapper: {write_result}"
        )


def _check_delegation_chain(body: dict) -> None:
    """The redeem step must produce a delegatee TCT and sub_write must succeed."""
    assert _step(body, "trust_researcher_writer") == {"trust": "established"}

    delegate = _step(body, "delegate")
    # The engine returns the delegator's /admin/delegate body verbatim under
    # this step. It must include a non-empty delegation_token and a
    # delegatee_aid pointing at sub-researcher.
    assert isinstance(delegate, dict), f"delegate output not a dict: {delegate!r}"
    assert delegate.get("delegation_token"), (
        f"delegation-chain: delegate produced no delegation_token: {delegate}"
    )
    assert delegate.get("delegatee_aid"), (
        f"delegation-chain: delegate produced no delegatee_aid: {delegate}"
    )

    redeem = _step(body, "redeem")
    # The redeemed body returned by writer's /aitp/delegation/redeem; presence
    # alone (step.complete fired) is enough — if redeem 4xx'd the step would
    # have raised and failed the run.
    assert isinstance(redeem, dict) and redeem, (
        f"delegation-chain: redeem produced empty output: {redeem!r}"
    )

    # sub_write: sub-researcher invokes writer.write.content using the
    # redeemed TCT. Reach == success means the call returned 2xx.
    sub_write = _step(body, "sub_write")
    if isinstance(sub_write, dict):
        assert not sub_write.get("error"), (
            f"delegation-chain: sub_write looks like an error wrapper: {sub_write}"
        )


def _check_revocation_demo(body: dict) -> None:
    """first_call succeeds, revoke records a jti, blocked_call observes 403."""
    first = _step(body, "first_call")
    if isinstance(first, dict):
        assert not first.get("error"), (
            f"revocation-demo: first_call should succeed, got {first}"
        )

    revoke = _step(body, "revoke")
    assert isinstance(revoke, dict) and revoke.get("revoked_jti"), (
        f"revocation-demo: revoke produced no revoked_jti: {revoke!r}"
    )

    blocked = _step(body, "blocked_call")
    assert blocked.get("status_code") == 403, (
        f"revocation-demo: expected blocked_call to observe 403, got "
        f"{blocked.get('status_code')!r}"
    )
    assert blocked.get("rejected") is True and blocked.get("matched") is True


def _check_revocation_via_cp(body: dict) -> None:
    """End-to-end CP revocation: revoke step must report both local and
    CP-side propagation, and the audience must observe a 403 *after* its
    own list-refresh — proving propagation through the public well-known
    list, not just a direct issuer back-channel."""
    first = _step(body, "first_call")
    if isinstance(first, dict):
        assert not first.get("error"), (
            f"revocation-via-cp: first_call should succeed, got {first}"
        )

    revoke = _step(body, "revoke")
    assert isinstance(revoke, dict) and revoke.get("revoked_jti"), (
        f"revocation-via-cp: revoke produced no revoked_jti: {revoke!r}"
    )
    assert revoke.get("published_to_cp") is True, (
        f"revocation-via-cp: jti was not published to the CP: {revoke!r}"
    )
    assert revoke.get("audience_revoked_count", 0) >= 1, (
        f"revocation-via-cp: audience did not pull the jti from CP: {revoke!r}"
    )

    blocked = _step(body, "blocked_call")
    assert blocked.get("status_code") == 403, (
        f"revocation-via-cp: expected 403 on blocked_call, got "
        f"{blocked.get('status_code')!r}"
    )
    assert blocked.get("rejected") is True and blocked.get("matched") is True

    # The CP-publish event must appear in the run event log so an operator
    # can correlate run → CP entry without inspecting the CP separately.
    event_types = {e.get("type") for e in (body.get("events") or [])}
    assert "revocation.published" in event_types, (
        f"revocation-via-cp: revocation.published missing from events: "
        f"{sorted(event_types)}"
    )


def _check_delegation_multihop(body: dict) -> None:
    """Two-hop delegation: each delegate step must produce a non-empty
    token, each redeem step must complete, and the terminal capability
    call (analyst → writer.write.content) must succeed."""
    assert _step(body, "trust_researcher_writer") == {"trust": "established"}

    for hop_id in ("delegate_to_sub", "delegate_to_analyst"):
        delegate = _step(body, hop_id)
        assert isinstance(delegate, dict) and delegate.get("delegation_token"), (
            f"delegation-multihop[{hop_id}]: missing delegation_token: {delegate!r}"
        )
        assert delegate.get("delegatee_aid"), (
            f"delegation-multihop[{hop_id}]: missing delegatee_aid: {delegate!r}"
        )

    for hop_id in ("redeem_sub", "redeem_analyst"):
        redeem = _step(body, hop_id)
        assert isinstance(redeem, dict) and redeem, (
            f"delegation-multihop[{hop_id}]: redeem produced empty output: {redeem!r}"
        )

    final = _step(body, "analyst_writes")
    if isinstance(final, dict):
        assert not final.get("error"), (
            f"delegation-multihop: terminal call rejected: {final}"
        )


def _check_key_rotation(body: dict) -> None:
    """Pre-rotation call succeeds; rotate emits old/new AID; post-rotation
    probe with the stale TCT observes a 403 because the TCT's declared
    issuer no longer matches the running agent."""
    assert _step(body, "handshake") == {"trust": "established"}

    first = _step(body, "first_write")
    if isinstance(first, dict):
        assert not first.get("error"), (
            f"key-rotation: pre-rotation write should succeed, got {first}"
        )

    rotated = _step(body, "writer_rotates")
    assert isinstance(rotated, dict), f"key-rotation: rotate output not dict: {rotated!r}"
    assert rotated.get("old_aid") and rotated.get("new_aid"), (
        f"key-rotation: rotate output missing old_aid/new_aid: {rotated!r}"
    )
    assert rotated["old_aid"] != rotated["new_aid"], (
        f"key-rotation: new AID identical to old: {rotated!r}"
    )

    stale = _step(body, "stale_write")
    assert stale.get("status_code") == 403, (
        f"key-rotation: expected stale TCT to be rejected, got "
        f"{stale.get('status_code')!r}"
    )
    assert stale.get("rejected") is True and stale.get("matched") is True

    event_types = {e.get("type") for e in (body.get("events") or [])}
    assert "identity.key.rotated" in event_types, (
        f"key-rotation: identity.key.rotated missing from events: "
        f"{sorted(event_types)}"
    )


def _check_scoped_capabilities(body: dict) -> None:
    """In-scope call succeeds, out-of-scope call observes 403."""
    assert _step(body, "scoped_handshake") == {"trust": "established"}

    out_of_scope = _step(body, "probe_out_of_scope")
    assert out_of_scope.get("status_code") == 403, (
        f"scoped-capabilities: expected probe_out_of_scope to observe 403, "
        f"got {out_of_scope.get('status_code')!r}"
    )
    assert out_of_scope.get("rejected") is True and out_of_scope.get("matched") is True

    in_scope = _step(body, "in_scope_call")
    if isinstance(in_scope, dict):
        assert not in_scope.get("error"), (
            f"scoped-capabilities: in_scope_call should succeed, got {in_scope}"
        )


SCENARIOS: list[ProtocolCase] = [
    ProtocolCase(
        ref="intra-org/trust-gate@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_trust_gate,
    ),
    ProtocolCase(
        ref="intra-org/delegation-chain@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_delegation_chain,
    ),
    ProtocolCase(
        ref="intra-org/revocation-demo@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_revocation_demo,
    ),
    ProtocolCase(
        ref="intra-org/scoped-capabilities@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_scoped_capabilities,
    ),
    ProtocolCase(
        ref="intra-org/revocation-via-cp@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_revocation_via_cp,
    ),
    ProtocolCase(
        ref="intra-org/delegation-multihop@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_delegation_multihop,
    ),
    ProtocolCase(
        ref="intra-org/key-rotation@1.0.0",
        inputs={"topic": "AITP self-test"},
        check=_check_key_rotation,
    ),
]


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(base_url=PLAYGROUND_URL, timeout=30.0) as c:
        try:
            r = c.get("/healthz")
        except httpx.HTTPError as exc:
            pytest.fail(f"playground not reachable at {PLAYGROUND_URL}: {exc}")
        assert r.status_code == 200, f"/healthz returned {r.status_code}: {r.text}"
        yield c


@pytest.fixture(scope="module")
def cp_client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {CP_API_KEY}"} if CP_API_KEY else {}
    with httpx.Client(base_url=CP_URL, timeout=10.0, headers=headers) as c:
        try:
            r = c.get("/api/readyz")
        except httpx.HTTPError as exc:
            pytest.fail(f"CP not reachable at {CP_URL}: {exc}")
        assert r.status_code == 200, f"/api/readyz: {r.status_code} {r.text}"
        yield c


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda c: c.ref)
def test_protocol_scenario(
    client: httpx.Client, cp_client: httpx.Client, case: ProtocolCase
) -> None:
    run_id = _start_run(client, case)
    body = _wait_for_terminal(client, run_id)

    assert body["status"] == "success", (
        f"{case.ref} did not succeed: status={body['status']!r} "
        f"error={body.get('error')!r}\nevents={body.get('events')}"
    )

    case.check(body)

    # Confirm CP ingestion of this run's events — same as the LLM suite,
    # we don't want a silent fallback to no-CP mode.
    deadline = time.time() + 15
    cp_types: set[str] = set()
    while time.time() < deadline:
        r = cp_client.get(
            "/api/events/history", params={"run_id": run_id, "limit": 200}
        )
        assert r.status_code == 200, f"CP history: {r.status_code} {r.text}"
        events = r.json().get("events", [])
        cp_types = {e.get("type") for e in events}
        if "run.complete" in cp_types:
            break
        time.sleep(0.5)
    assert "run.complete" in cp_types, (
        f"{case.ref}: CP did not receive run.complete for run_id={run_id} "
        f"(got types {sorted(cp_types)})"
    )
