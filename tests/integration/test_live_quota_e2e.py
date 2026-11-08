"""Per-IP LLM quota against a real provider, on the live compose stack.

Runs only inside ``scripts/live-integration.sh`` (``docker-compose.live.yml``
sets ``AITP_LIVE_LLM=1`` and ``LLM_QUOTA_PER_IP_PER_DAY=4``), and FIRST, so
the global daily counter is still unspent and any denial here can only come
from the per-IP counter.

One research-and-write run from one subject IP: a keyed request carrying
``X-AITP-Client-IP`` (the console's shape). The researcher asks for 4 units
(0 + 4 <= 4, allowed, real provider calls); the writer asks for 1 (4 + 1 > 4,
refused, deterministic stub). The run must succeed, flagged once with
``per_ip_daily``. Every other live test sends the key with no client-IP
header, so it charges only the global counters and is never refused here.
"""
from __future__ import annotations

import os
import random
import time

import httpx
import pytest

pytestmark = pytest.mark.live_llm

PLAYGROUND_URL = os.environ.get("PLAYGROUND_URL", "http://localhost:8000")
PLAYGROUND_API_KEY = os.environ.get("PLAYGROUND_API_KEY", "")
#: Must equal the playground's LLM_QUOTA_PER_IP_PER_DAY; docker-compose.live.yml
#: sets both from the same value.
PER_IP_DAILY = int(os.environ.get("LIVE_LLM_QUOTA_PER_IP_PER_DAY", "4"))
#: Units the two agents ask for (agents/researcher/crew.py RESEARCH_LLM_CALLS,
#: the writer's single call).
RESEARCH_CALLS = 4
WRITE_CALLS = 1

_TERMINAL = {"success", "failed", "cancelled"}
_RUN_DEADLINE_SECS = 300
_RESEARCHER_STUB = "continues to attract significant research interest"
_WRITER_STUB = "# Article ("


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    return str(value)


def _provider_trouble(events: list[dict]) -> str | None:
    """A provider-side 429/5xx in the run's events: report flaky, don't retry."""
    for e in events:
        if e.get("type") != "llm.failed":
            continue
        blob = _text(e)
        for code in ("429", "500", "502", "503", "504", "529", "rate limit", "overloaded"):
            if code in blob.lower():
                return blob[:300]
    return None


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    assert PLAYGROUND_API_KEY, "PLAYGROUND_API_KEY must be set (docker-compose.test.yml)"
    headers = {"Authorization": f"Bearer {PLAYGROUND_API_KEY}"}
    with httpx.Client(base_url=PLAYGROUND_URL, timeout=30.0, headers=headers) as c:
        r = c.get("/healthz")
        assert r.status_code == 200, f"/healthz returned {r.status_code}"
        yield c


def test_per_ip_quota_refuses_the_writer_and_the_run_completes(client: httpx.Client) -> None:
    assert RESEARCH_CALLS <= PER_IP_DAILY < RESEARCH_CALLS + WRITE_CALLS, (
        "this test needs a per-IP limit that admits the researcher and refuses the writer"
    )
    # TEST-NET-3: never a real visitor, fresh per invocation.
    subject_ip = f"203.0.113.{random.randint(1, 254)}"
    posted = client.post(
        "/runs",
        headers={"X-AITP-Client-IP": subject_ip},
        json={
            "scenario_ref": "intra-org/research-and-write@1.0.0",
            "inputs": {"topic": "AITP per-IP quota live check"},
        },
    )
    assert posted.status_code == 202, f"POST /runs: {posted.status_code} {posted.text}"
    run_id = posted.json()["run_id"]

    deadline = time.time() + _RUN_DEADLINE_SECS
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body.get("status") in _TERMINAL:
            break
        time.sleep(1.0)
    else:
        pytest.fail(f"run {run_id} did not finish in {_RUN_DEADLINE_SECS}s")

    events = body.get("events") or []
    trouble = _provider_trouble(events)
    if trouble:
        pytest.fail(f"FLAKY (provider 429/5xx, not retried): {trouble}")

    assert body["status"] == "success", f"run {run_id}: {body.get('error')!r}"
    assert body["llm_quota_exceeded"] is True
    quota = body["llm_quota"]
    assert quota["reason"] == "quota_exceeded", quota
    # The per-IP counter refused, not the global cap (still unspent: this
    # test runs first).
    assert quota["quota"] == "per_ip_daily", quota
    assert quota["limit"] == PER_IP_DAILY, quota
    assert quota["resets_at"]
    # Only the writer's unit fell back to the stub.
    assert body["llm_calls_on_mock"] == WRITE_CALLS, body["llm_calls_on_mock"]

    quota_events = [e for e in events if e.get("type") == "llm.quota_exceeded"]
    assert len(quota_events) == 1, quota_events
    assert quota_events[0].get("agent_id") == "writer", quota_events[0]
    assert "llm.failed" not in {e.get("type") for e in events}

    outputs = body.get("outputs") or {}
    research = _text(outputs.get("research"))
    write = _text(outputs.get("write"))
    assert research.strip(), "research produced no output"
    assert _RESEARCHER_STUB not in research, "the researcher ran its stub: no real LLM call"
    assert _WRITER_STUB in write, "the writer was refused, so its output must be the stub"
