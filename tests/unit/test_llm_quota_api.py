"""POST /internal/llm-quota: the gate agents ask before real LLM calls, and
the visible flag (run summary + one llm.quota_exceeded event) on a denial."""
from __future__ import annotations

import threading
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from aitp_playground.api.quota import record_llm_fallback
from aitp_playground.config import Settings
from aitp_playground.main import create_app
from aitp_playground.observability.metrics import (
    LLM_QUOTA_CHARGED_TOTAL,
    LLM_QUOTA_DENIALS_TOTAL,
    LLM_QUOTA_STORE_UP,
    metrics,
)
from aitp_playground.quota import MemoryQuotaStore, UnavailableQuotaStore
from aitp_playground.runner.store import RunStore, SqliteRunStore
from tests._auth import AUTH
from tests._quota_contract import Clock, limits

LOOPBACK = ("127.0.0.1", 50123)
IP = "198.51.100.7"
DAY = date(2026, 11, 7)


def _app(store=None):
    app = create_app(Settings())
    app.state.quota_store = store or MemoryQuotaStore(limits(), clock=Clock())
    return app


def _client(app, client=LOOPBACK) -> TestClient:
    return TestClient(app, headers=AUTH, client=client)


def _tok(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.telemetry_token}"}


def _run(app, rid="run-q1", subject_ip=IP) -> str:
    app.state.run_store.upsert(rid, {
        "run_id": rid, "status": "running", "scenario_ref": "intra-org/x@1.0.0",
        "subject_ip": subject_ip,
    })
    return rid


def _ask(c, app, rid, calls=1, capability="write.content", **extra):
    body = {"run_id": rid, "capability": capability, "calls": calls, **extra}
    return c.post("/internal/llm-quota", headers=_tok(app), json=body)


def _quota_events(c, rid):
    return [e for e in c.get(f"/runs/{rid}").json()["events"] if e.get("type") == "llm.quota_exceeded"]


def _counter(name, labels=()):
    return metrics.snapshot()["counters"].get((name, labels), 0.0)


# --- access -------------------------------------------------------------------

def test_non_loopback_peer_is_refused():
    app = _app()
    c = _client(app, client=("203.0.113.5", 4000))
    assert _ask(c, app, _run(app)).status_code == 403


def test_loopback_through_a_proxy_is_refused():
    app = _app()
    c = _client(app)
    r = c.post(
        "/internal/llm-quota", json={"run_id": _run(app), "capability": "x", "calls": 1},
        headers={**_tok(app), "X-Forwarded-For": "203.0.113.5"},
    )
    assert r.status_code == 403


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_missing_or_wrong_token_is_refused(headers):
    app = _app()
    c = TestClient(app, client=LOOPBACK)
    r = c.post("/internal/llm-quota", json={"run_id": None, "capability": "x", "calls": 1},
               headers=headers)
    assert r.status_code == 401


def test_the_api_key_is_not_enough():
    app = _app()
    c = TestClient(app, client=LOOPBACK)
    r = c.post("/internal/llm-quota", json={"run_id": None, "capability": "x", "calls": 1},
               headers=AUTH)
    assert r.status_code == 401


@pytest.mark.parametrize("calls", [0, -1, 101])
def test_calls_out_of_range_is_rejected(calls):
    app = _app()
    assert _ask(_client(app), app, None, calls=calls).status_code == 422


# --- charging -----------------------------------------------------------------

def test_allowed_charge_uses_the_run_records_ip_and_counts_calls():
    store = MemoryQuotaStore(limits(), clock=Clock())
    app = _app(store)
    c = _client(app)
    rid = _run(app)
    before = _counter(LLM_QUOTA_CHARGED_TOTAL)
    # A subject_ip in the body is not a field: the agent can't pick who pays.
    r = _ask(c, app, rid, calls=4, subject_ip="192.0.2.99")
    assert r.status_code == 200
    assert r.json() == {"allowed": True, "reason": None, "quota": None, "limit": None,
                        "resets_at": None}
    assert store.usage("ip_day", IP, DAY) == 4
    assert store.usage("ip_day", "192.0.2.99", DAY) == 0
    assert store.usage("global_day", "*", DAY) == 4
    assert _counter(LLM_QUOTA_CHARGED_TOTAL) - before == 4
    run = c.get(f"/runs/{rid}").json()
    assert run["llm_quota_exceeded"] is False and run["llm_calls_on_mock"] == 0


def test_keyless_run_charges_global_counters_only():
    store = MemoryQuotaStore(limits(), clock=Clock())
    app = _app(store)
    rid = _run(app, subject_ip=None)
    assert _ask(_client(app), app, rid, calls=2).json()["allowed"]
    assert store.usage("global_day", "*", DAY) == 2
    assert not any(k[0] == "ip_day" for k in store._counts)


@pytest.mark.parametrize("rid", [None, "hosted-abc123"])
def test_hosted_or_runless_agents_charge_global_only(rid):
    store = MemoryQuotaStore(limits(), clock=Clock())
    app = _app(store)
    r = _ask(_client(app), app, rid, calls=1)
    assert r.status_code == 200 and r.json()["allowed"]
    assert store.usage("global_day", "*", DAY) == 1
    assert app.state.run_store.get("hosted-abc123") is None


def test_unknown_run_is_404_and_charges_nothing():
    store = MemoryQuotaStore(limits(), clock=Clock())
    app = _app(store)
    r = _ask(_client(app), app, "run-nope")
    assert r.status_code == 404
    assert store.usage("global_day", "*", DAY) == 0
    assert app.state.run_store.get("run-nope") is None


# --- denial: the visible flag ------------------------------------------------

def test_denial_flags_the_run_once_and_counts_mock_calls():
    app = _app(MemoryQuotaStore(limits(ip_day=1), clock=Clock()))
    c = _client(app)
    rid = _run(app)
    before = _counter(LLM_QUOTA_DENIALS_TOTAL, (("reason", "quota_exceeded"),))
    assert _ask(c, app, rid, calls=1).json()["allowed"]
    d = _ask(c, app, rid, calls=2, capability="analyze.data", agent_id="analyzer").json()
    assert d == {"allowed": False, "reason": "quota_exceeded", "quota": "per_ip_daily",
                 "limit": 1, "resets_at": "2026-11-08T00:00:00Z"}
    assert not _ask(c, app, rid, calls=4, capability="research.query").json()["allowed"]

    run = c.get(f"/runs/{rid}").json()
    assert run["llm_quota_exceeded"] is True
    assert run["llm_quota"] == {"reason": "quota_exceeded", "quota": "per_ip_daily",
                                "limit": 1, "resets_at": "2026-11-08T00:00:00Z"}
    assert run["llm_calls_on_mock"] == 6
    events = _quota_events(c, rid)
    assert len(events) == 1
    ev = events[0]
    assert (ev["code"], ev["quota"], ev["limit"], ev["resets_at"], ev["capability"],
            ev["agent_id"], ev["calls"]) == (
        "quota_exceeded", "per_ip_daily", 1, "2026-11-08T00:00:00Z", "analyze.data",
        "analyzer", 2)
    status = c.get(f"/runs/{rid}/status").json()
    assert status["llm_quota_exceeded"] and status["llm_calls_on_mock"] == 6
    listed = [r for r in c.get("/runs").json()["runs"] if r["run_id"] == rid][0]
    assert listed["llm_quota"]["resets_at"] == "2026-11-08T00:00:00Z"
    assert _counter(LLM_QUOTA_DENIALS_TOTAL, (("reason", "quota_exceeded"),)) - before == 2


def test_denial_after_the_run_finished_is_still_counted():
    store = MemoryQuotaStore(limits(g_day=0), clock=Clock())
    app = _app(store)
    c = _client(app)
    rid = _run(app)
    app.state.run_store.upsert(rid, {"status": "success"})
    assert not _ask(c, app, rid).json()["allowed"]
    run = c.get(f"/runs/{rid}").json()
    assert run["status"] == "success" and run["llm_quota_exceeded"]


def test_store_unavailable_denies_with_null_limit_and_reports_down():
    app = _app(UnavailableQuotaStore(limits()))
    c = _client(app)
    rid = _run(app)
    d = _ask(c, app, rid).json()
    assert (d["allowed"], d["reason"], d["limit"], d["resets_at"]) == (
        False, "quota_store_unavailable", None, None)
    assert metrics.snapshot()["gauges"][(LLM_QUOTA_STORE_UP, ())] == 0.0
    run = c.get(f"/runs/{rid}").json()
    assert run["llm_quota"]["reason"] == "quota_store_unavailable"
    assert run["llm_quota"]["resets_at"] is None


def test_store_outage_logs_once_per_transition(caplog):
    app = _app(UnavailableQuotaStore(limits()))
    c = _client(app)
    metrics.set(LLM_QUOTA_STORE_UP, 1.0)
    with caplog.at_level("INFO", logger="aitp_playground.quota"):
        for _ in range(3):
            assert not _ask(c, app, None).json()["allowed"]
        app.state.quota_store = MemoryQuotaStore(limits(), clock=Clock())
        for _ in range(2):
            assert _ask(c, app, None).json()["allowed"]
    msgs = [r.getMessage() for r in caplog.records if "quota store" in r.getMessage()]
    assert msgs == [
        "LLM quota store unavailable: refusing real LLM calls (fail closed)",
        "LLM quota store reachable",
    ]


def test_store_up_gauge_is_one_after_a_good_check():
    app = _app()
    assert _ask(_client(app), app, None).json()["allowed"]
    assert metrics.snapshot()["gauges"][(LLM_QUOTA_STORE_UP, ())] == 1.0


def test_metrics_endpoint_exposes_quota_series():
    app = _app()
    text = _client(app).get("/metrics").text
    for name in (LLM_QUOTA_CHARGED_TOTAL, LLM_QUOTA_DENIALS_TOTAL):
        assert name in text


async def test_concurrent_denials_emit_one_event():
    app = _app(MemoryQuotaStore(limits(g_day=0), clock=Clock()))
    rid = _run(app)
    transport = httpx.ASGITransport(app=app, client=LOOPBACK)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        import asyncio
        rs = await asyncio.gather(*(
            c.post("/internal/llm-quota", headers=_tok(app),
                   json={"run_id": rid, "capability": "x", "calls": 1})
            for _ in range(20)
        ))
    assert all(r.status_code == 200 and not r.json()["allowed"] for r in rs)
    rec = app.state.run_store.get(rid)
    assert rec["llm_calls_on_mock"] == 20
    assert sum(e.get("type") == "llm.quota_exceeded" for e in rec["events"]) == 1


def test_record_llm_fallback_is_single_shot_across_threads():
    store = RunStore()
    store.upsert("r", {"run_id": "r", "status": "running"})
    emitted: list[bool] = []
    barrier = threading.Barrier(16)

    def hit():
        barrier.wait()
        emitted.append(record_llm_fallback(store, "r", reason="quota_exceeded", calls=1))

    threads = [threading.Thread(target=hit) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert emitted.count(True) == 1
    assert store.get("r")["llm_calls_on_mock"] == 16
    assert sum(e["type"] == "llm.quota_exceeded" for e in store.get("r")["events"]) == 1


# --- agent-reported check failures (telemetry) --------------------------------

def test_agent_reported_check_failure_flags_the_run_once():
    app = _app()
    c = _client(app)
    rid = _run(app)
    body = {"run_id": rid, "type": "llm.quota_exceeded", "code": "quota_check_failed",
            "capability": "write.content", "calls": 1, "agent_id": "writer"}
    for _ in range(3):
        assert c.post("/internal/telemetry", headers=_tok(app), json=body).status_code == 200
    run = c.get(f"/runs/{rid}").json()
    assert run["llm_quota"]["reason"] == "quota_check_failed"
    assert run["llm_calls_on_mock"] == 3
    assert len(_quota_events(c, rid)) == 1


def test_agent_reported_reason_is_pinned_whatever_the_body_says():
    """The body's code is never a metric label or the run's reason: only
    quota_check_failed can come in through telemetry."""
    app = _app()
    c = _client(app)
    rid = _run(app)
    before = _counter(LLM_QUOTA_DENIALS_TOTAL, (("reason", "quota_check_failed"),))
    for code in ("quota_exceeded", "x" * 50):
        c.post("/internal/telemetry", headers=_tok(app),
               json={"run_id": rid, "type": "llm.quota_exceeded", "code": code, "calls": 1})
    assert c.get(f"/runs/{rid}").json()["llm_quota"]["reason"] == "quota_check_failed"
    assert _counter(LLM_QUOTA_DENIALS_TOTAL, (("reason", "quota_check_failed"),)) - before == 2
    assert "x" * 50 not in c.get("/metrics").text


def test_agent_duplicate_after_a_server_denial_is_dropped():
    app = _app(MemoryQuotaStore(limits(g_day=0), clock=Clock()))
    c = _client(app)
    rid = _run(app)
    assert not _ask(c, app, rid).json()["allowed"]
    c.post("/internal/telemetry", headers=_tok(app),
           json={"run_id": rid, "type": "llm.quota_exceeded", "calls": 2})
    events = _quota_events(c, rid)
    assert len(events) == 1 and events[0]["code"] == "quota_exceeded"


def test_agent_quota_event_for_an_unknown_run_creates_nothing():
    app = _app()
    c = _client(app)
    c.post("/internal/telemetry", headers=_tok(app),
           json={"run_id": "run-ghost", "type": "llm.quota_exceeded", "calls": 1})
    assert app.state.run_store.get("run-ghost") is None


# --- RunStore.update ----------------------------------------------------------

def test_update_unknown_run_is_false_and_never_creates():
    store = RunStore()
    called = []
    assert store.update("nope", called.append) is False
    assert called == [] and store.get("nope") is None


def test_update_keeps_events_and_persists_in_sqlite(tmp_path):
    db = str(tmp_path / "runs.db")
    store = SqliteRunStore(db)
    store.upsert("r", {"run_id": "r", "status": "running"})
    store.append_event("r", {"type": "run.started"})

    def fn(rec):
        rec["llm_quota_exceeded"] = True
        rec["events"] = []  # ignored: update never touches events

    assert store.update("r", fn)
    assert store.get("r")["events"] == [{"type": "run.started"}]
    store.close()
    again = SqliteRunStore(db)
    assert again.get("r")["llm_quota_exceeded"] is True
    assert len(again.get("r")["events"]) == 1
    again.close()


@pytest.mark.parametrize("header,charged_ip", [
    ("2001:db8:1:2:aaaa::9", "2001:db8:1:2::/64"),  # normalised, as the launch saw it
    (None, None),                                   # keyed, no header: global only
])
def test_launch_ip_flows_from_post_runs_to_the_quota_charge(header, charged_ip):
    """The seam between the gate and the quota: the trusted client IP a keyed
    launch carries is what a later quota check on that run charges."""
    store = MemoryQuotaStore(limits(), clock=Clock())
    app = _app(store)

    async def _noop(**_kwargs):
        return None

    app.state.runner.run = _noop
    c = _client(app)
    headers = {"X-AITP-Client-IP": header} if header else {}
    r = c.post("/runs", headers=headers,
               json={"scenario_ref": "intra-org/trust-gate@1.0.0", "inputs": {"topic": "t"}})
    assert r.status_code in (200, 201, 202), r.text
    rid = r.json()["run_id"]
    assert _ask(c, app, rid, calls=3).json()["allowed"]
    assert store.usage("global_day", "*", DAY) == 3
    ip_rows = {k[1]: v for k, v in store._counts.items() if k[0] == "ip_day"}
    assert ip_rows == ({charged_ip: 3} if charged_ip else {})
