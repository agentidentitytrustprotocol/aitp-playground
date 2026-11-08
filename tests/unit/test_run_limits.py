"""Run launch limits: the per-IP window, the concurrency cap, the deadline,
and their wiring on ``POST /runs`` and hosted invoke. Time comes from a fake
clock; runs are stubbed, never real scenarios."""
from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from aitp_playground.api import runs as runs_api
from aitp_playground.config import Settings
from aitp_playground.errors import RunLimitExceeded
from aitp_playground.limits import RunLimiter, SpendLimits, TokenBucket
from aitp_playground.main import create_app
from aitp_playground.runner.store import RunStore
from tests._auth import AUTH, TEST_API_KEY


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: FakeClock, *, per_ip=5, window=600, concurrent=3, hold=1020.0) -> RunLimiter:
    return RunLimiter(
        per_ip=per_ip, window_secs=window, max_concurrent=concurrent, max_hold_secs=hold,
        clock=clock,
    )


# --- RunLimiter -----------------------------------------------------------------

def test_window_admits_n_then_refuses_with_retry_after_to_the_oldest_expiry() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    for i in range(5):
        lim.reserve("198.51.100.7", slot=False)
        clock.now += 10  # stamps at 1000, 1010, ..., 1040
    with pytest.raises(RunLimitExceeded) as exc:
        lim.reserve("198.51.100.7", slot=False)  # now = 1050
    assert exc.value.code == "run_rate_limited"
    assert exc.value.limit == 5
    assert exc.value.retry_after == 1000 + 600 - 1050
    # Another IP is unaffected.
    lim.reserve("198.51.100.8", slot=False)


def test_window_frees_exactly_when_the_oldest_launch_ages_out() -> None:
    clock = FakeClock()
    lim = _limiter(clock, per_ip=1, window=600)
    lim.reserve("a", slot=False)
    clock.now += 599.5
    with pytest.raises(RunLimitExceeded) as exc:
        lim.reserve("a", slot=False)
    assert exc.value.retry_after == 1  # ceil(0.5), floored at 1
    clock.now += 0.5
    lim.reserve("a", slot=False)


def test_no_subject_ip_is_never_window_limited() -> None:
    lim = _limiter(FakeClock(), per_ip=1)
    for _ in range(10):
        lim.reserve(None, slot=False)


def test_concurrency_cap_and_release() -> None:
    lim = _limiter(FakeClock(), concurrent=2)
    a = lim.reserve(None, slot=True)
    lim.reserve(None, slot=True)
    with pytest.raises(RunLimitExceeded) as exc:
        lim.reserve(None, slot=True)
    assert (exc.value.code, exc.value.limit, exc.value.retry_after) == ("max_concurrent_runs", 2, 10)
    a.release()
    a.release()  # idempotent: frees one slot, not two
    assert lim.active == 1
    lim.reserve(None, slot=True)
    with pytest.raises(RunLimitExceeded):
        lim.reserve(None, slot=True)


def test_concurrency_refusal_does_not_use_a_window_entry() -> None:
    lim = _limiter(FakeClock(), per_ip=1, concurrent=1)
    lim.reserve(None, slot=True)
    with pytest.raises(RunLimitExceeded) as exc:
        lim.reserve("a", slot=True)
    assert exc.value.code == "max_concurrent_runs"
    lim.reserve("a", slot=False)  # the window entry is still free


def test_cancel_hands_back_the_slot_and_the_window_entry() -> None:
    lim = _limiter(FakeClock(), per_ip=1, concurrent=1)
    r = lim.reserve("a", slot=True)
    undone: list[int] = []
    r.on_cancel(lambda: undone.append(1))
    r.cancel()
    r.cancel()
    assert lim.active == 0
    assert undone == [1]  # registered undo runs once, however often cancel is called
    lim.reserve("a", slot=True)


def test_slot_without_a_window_takes_no_stamp() -> None:
    lim = _limiter(FakeClock(), per_ip=1)
    r = lim.reserve("a", slot=False)
    assert not r.holds_slot
    assert lim.active == 0


def test_a_leaked_slot_is_reclaimed_after_max_hold() -> None:
    clock = FakeClock()
    lim = _limiter(clock, concurrent=1, hold=100)
    lim.reserve(None, slot=True)  # never released
    clock.now += 99
    with pytest.raises(RunLimitExceeded):
        lim.reserve(None, slot=True)
    clock.now += 1
    lim.reserve(None, slot=True)


def test_idle_windows_are_pruned_past_the_threshold() -> None:
    clock = FakeClock()
    lim = _limiter(clock, window=10)
    for i in range(4097):
        lim.reserve(f"ip{i}", slot=False)
    assert len(lim._windows) == 4097  # nothing expired yet, nothing dropped
    clock.now += 10
    lim.reserve("fresh", slot=False)
    assert list(lim._windows) == ["fresh"]


def test_concurrent_reservations_never_exceed_the_cap() -> None:
    lim = _limiter(FakeClock(), per_ip=1000, concurrent=3)
    admitted: list[object] = []
    refused: list[str] = []
    barrier = threading.Barrier(32)

    def go() -> None:
        barrier.wait()
        try:
            admitted.append(lim.reserve("a", slot=True))
        except RunLimitExceeded as exc:
            refused.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 3
    assert refused == ["max_concurrent_runs"] * 29
    assert len(lim._windows["a"]) == 3


def test_from_settings_reads_the_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_RATE_LIMIT_PER_IP", "7")
    monkeypatch.setenv("RUN_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "2")
    monkeypatch.setenv("RUN_DEADLINE_SECONDS", "30")
    lim = RunLimiter.from_settings(Settings())
    assert (lim.per_ip, lim.window, lim.max_concurrent, lim.max_hold) == (7, 60, 2, 150.0)


def test_defaults_match_the_rollout_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RUN_RATE_LIMIT_PER_IP", "RUN_RATE_LIMIT_WINDOW_SECONDS",
                 "MAX_CONCURRENT_RUNS", "RUN_DEADLINE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert (s.run_rate_limit_per_ip, s.run_rate_limit_window_seconds,
            s.max_concurrent_runs, s.run_deadline_seconds) == (5, 600, 3, 900.0)


@pytest.mark.parametrize("field", ["run_rate_limit_per_ip", "max_concurrent_runs",
                                   "run_rate_limit_window_seconds", "run_deadline_seconds"])
def test_non_positive_limits_refuse_to_load(field: str) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: 0})


# --- HTTP wiring ------------------------------------------------------------------

_RUN = {"scenario_ref": "intra-org/trust-gate@1.0.0", "inputs": {"topic": "t"}}
CONSOLE_IP = "198.51.100.7"


def _app(clock: FakeClock, runner_run=None, **overrides):
    settings = Settings(
        run_rate_limit_per_ip=overrides.pop("run_rate_limit_per_ip", 5),
        max_concurrent_runs=overrides.pop("max_concurrent_runs", 3),
        **overrides,
    )
    app = create_app(settings)
    app.state.spend_limits = SpendLimits(settings, clock=clock)
    app.state.run_limiter = RunLimiter.from_settings(settings, clock=clock)

    async def _noop(**_kwargs):
        return None

    app.state.runner.run = runner_run or _noop
    return app


def _keyed(ip: str | None = CONSOLE_IP) -> dict[str, str]:
    h = {"x-api-key": TEST_API_KEY}
    if ip is not None:
        h["X-AITP-Client-IP"] = ip
    return h


def test_sixth_launch_from_one_ip_is_429_with_body_and_retry_after() -> None:
    clock = FakeClock()
    client = TestClient(_app(clock))
    for _ in range(5):
        assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202
        clock.now += 30
    r = client.post("/runs", json=_RUN, headers=_keyed())
    assert r.status_code == 429
    assert r.json() == {
        "code": "run_rate_limited", "limit": 5, "retry_after": 600 - 150,
        "detail": "at most 5 run launches per 600s from one client IP",
    }
    assert r.headers["Retry-After"] == "450"
    # Another visitor through the same key is unaffected.
    assert client.post("/runs", json=_RUN, headers=_keyed("198.51.100.8")).status_code == 202


def test_ipv6_visitors_in_one_64_share_a_window() -> None:
    client = TestClient(_app(FakeClock(), run_rate_limit_per_ip=1))
    assert client.post("/runs", json=_RUN, headers=_keyed("2001:db8:1:2::1")).status_code == 202
    r = client.post("/runs", json=_RUN, headers=_keyed("2001:db8:1:2:ffff::9"))
    assert r.status_code == 429
    assert r.json()["code"] == "run_rate_limited"


def test_keyed_launches_without_the_header_are_never_per_ip_limited() -> None:
    client = TestClient(_app(FakeClock(), run_rate_limit_per_ip=1))
    for _ in range(4):
        assert client.post("/runs", json=_RUN, headers=_keyed(None)).status_code == 202


def test_a_spoofed_header_without_a_key_is_401_and_counts_nothing() -> None:
    app = _app(FakeClock(), run_rate_limit_per_ip=1)
    bare = TestClient(app)
    for _ in range(3):
        r = bare.post("/runs", json=_RUN, headers={"X-AITP-Client-IP": CONSOLE_IP})
        assert r.status_code == 401
    assert app.state.run_limiter._windows == {}
    assert TestClient(app).post("/runs", json=_RUN, headers=_keyed()).status_code == 202


def test_trusted_header_launches_skip_the_per_key_bucket() -> None:
    clock = FakeClock()
    app = _app(clock)
    limits = app.state.spend_limits
    limits.per_key = TokenBucket(rate_per_sec=0, burst=1, clock=clock)
    client = TestClient(app)
    for i in range(3):
        r = client.post("/runs", json=_RUN, headers=_keyed(f"198.51.100.{i + 1}"))
        assert r.status_code == 202
    # Without the header the per-key bucket applies again.
    assert client.post("/runs", json=_RUN, headers=_keyed(None)).status_code == 202
    assert client.post("/runs", json=_RUN, headers=_keyed(None)).status_code == 429


def test_per_ip_bucket_keys_on_the_subject_ip_when_trusted() -> None:
    clock = FakeClock()
    app = _app(clock)
    app.state.spend_limits.per_ip = TokenBucket(rate_per_sec=0, burst=1, clock=clock)
    client = TestClient(app)
    assert client.post("/runs", json=_RUN, headers=_keyed("198.51.100.1")).status_code == 202
    assert client.post("/runs", json=_RUN, headers=_keyed("198.51.100.2")).status_code == 202
    r = client.post("/runs", json=_RUN, headers=_keyed("198.51.100.1"))
    assert r.status_code == 429


def test_fourth_concurrent_run_is_429_and_does_not_burn_the_hourly_cap() -> None:
    clock = FakeClock()
    app = _app(clock)
    lim: RunLimiter = app.state.run_limiter
    held = [lim.reserve(None, slot=True) for _ in range(3)]  # three runs in flight
    client = TestClient(app)
    r = client.post("/runs", json=_RUN, headers=_keyed())
    assert r.status_code == 429
    assert r.json()["code"] == "max_concurrent_runs"
    assert r.json()["limit"] == 3
    assert r.headers["Retry-After"] == "10"
    assert len(app.state.spend_limits.hourly._stamps) == 0
    assert not lim._windows.get(CONSOLE_IP)
    held[0].release()
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202


def test_hourly_refusal_hands_back_the_slot_and_the_window_entry() -> None:
    clock = FakeClock()
    app = _app(clock, max_runs_per_hour=1, run_rate_limit_per_ip=2)
    client = TestClient(app)
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202
    r = client.post("/runs", json=_RUN, headers=_keyed())
    assert r.status_code == 429
    assert "hourly" in r.json()["detail"]
    lim: RunLimiter = app.state.run_limiter
    assert lim.active == 0
    assert len(lim._windows[CONSOLE_IP]) == 1


@pytest.mark.parametrize("outcome", ["success", "crash"])
def test_slot_is_freed_when_the_run_ends(outcome: str) -> None:
    async def run(**kw):
        if outcome == "crash":
            raise RuntimeError("boom")
        return None

    app = _app(FakeClock(), runner_run=run, max_concurrent_runs=1)
    client = TestClient(app)
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202
    assert app.state.run_limiter.active == 0
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202


def test_invalid_body_hands_back_the_slot_and_window() -> None:
    app = _app(FakeClock(), max_concurrent_runs=1, run_rate_limit_per_ip=1)
    client = TestClient(app)
    r = client.post("/runs", json={"inputs": {}}, headers=_keyed())
    assert r.status_code == 422
    lim: RunLimiter = app.state.run_limiter
    assert lim.active == 0
    assert CONSOLE_IP not in lim._windows
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202


def test_record_error_before_handoff_hands_back_the_slot(monkeypatch) -> None:
    app = _app(FakeClock(), max_concurrent_runs=1)
    store = app.state.run_store

    def broken(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "upsert", broken)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 500
    assert app.state.run_limiter.active == 0


def test_failed_keyed_launch_refunds_the_hourly_slot(monkeypatch) -> None:
    """A keyed launch that fails before handoff (422 body, record error) leaves
    the global hourly count unchanged; the cap of 1 still admits a real run."""
    app = _app(FakeClock(), max_runs_per_hour=1)
    hourly = app.state.spend_limits.hourly
    client = TestClient(app, raise_server_exceptions=False)

    assert client.post("/runs", json={"inputs": {}}, headers=_keyed()).status_code == 422
    assert len(hourly._stamps) == 0

    store = app.state.run_store
    real_upsert = store.upsert

    def broken(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "upsert", broken)
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 500
    assert len(hourly._stamps) == 0

    monkeypatch.setattr(store, "upsert", real_upsert)
    assert client.post("/runs", json=_RUN, headers=_keyed()).status_code == 202
    assert len(hourly._stamps) == 1  # a handed-off run keeps its hourly slot
    r = client.post("/runs", json=_RUN, headers=_keyed())
    assert r.status_code == 429 and "hourly" in r.json()["detail"]


def test_subject_ip_is_stored_privately_on_the_run() -> None:
    app = _app(FakeClock())
    client = TestClient(app)
    run_id = client.post("/runs", json=_RUN, headers=_keyed()).json()["run_id"]
    assert app.state.run_store.get(run_id)["subject_ip"] == CONSOLE_IP
    body = client.get(f"/runs/{run_id}", headers=AUTH).json()
    assert "subject_ip" not in body
    listed = client.get("/runs", headers=AUTH).json()
    assert "subject_ip" not in str(listed)
    other = client.post("/runs", json=_RUN, headers=_keyed(None)).json()["run_id"]
    assert app.state.run_store.get(other)["subject_ip"] is None


def test_hosted_invoke_uses_the_window_but_no_slot() -> None:
    app = _app(FakeClock(), run_rate_limit_per_ip=1, max_concurrent_runs=1)
    client = TestClient(app)
    r = client.post("/hosted-agents/nope/invoke", json={"peer_port": 1, "capability": "c"},
                    headers=_keyed())
    assert r.status_code == 404  # past the limits, no such agent
    lim: RunLimiter = app.state.run_limiter
    assert lim.active == 0
    r = client.post("/hosted-agents/nope/invoke", json={"peer_port": 1, "capability": "c"},
                    headers=_keyed())
    assert r.status_code == 429
    assert r.json()["code"] == "run_rate_limited"


def test_flag_off_keyless_launch_is_still_401() -> None:
    client = TestClient(_app(FakeClock()))
    r = client.post("/runs", json=_RUN)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


# --- deadline -------------------------------------------------------------------

class _StubRunner:
    """Just enough of ScenarioRunner for ``_run_in_background``."""

    def __init__(self, store: RunStore, run) -> None:
        self.store = store
        self.run = run
        self.teardowns: list[str] = []

    def schedule_webhook_teardown(self, run_id: str, **_kw) -> None:
        self.teardowns.append(run_id)


def _body() -> runs_api.RunRequest:
    return runs_api.RunRequest(**_RUN)


async def test_deadline_ends_the_run_failed_frees_the_slot_and_tears_down() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    cleaned = asyncio.Event()

    async def hang(**_kw):
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.set()  # the engine's finally kills the agents here

    runner = _StubRunner(store, hang)
    lim = _limiter(FakeClock(), concurrent=1)
    reservation = lim.reserve(None, slot=True)
    await runs_api._run_in_background(runner, "r1", _body(), reservation, 0.05)

    assert cleaned.is_set()
    assert lim.active == 0
    rec = store.get("r1")
    assert rec["status"] == "failed"
    assert rec["error"].startswith("run_deadline_exceeded")
    failed = [e for e in rec["events"] if e["type"] == "run.failed"]
    assert len(failed) == 1 and failed[0]["code"] == "run_deadline_exceeded"
    assert runner.teardowns == ["r1"]


async def test_deadline_after_a_cancel_keeps_cancelled_and_emits_nothing() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "cancelled", "events": []})

    async def hang(**_kw):
        await asyncio.sleep(3600)

    runner = _StubRunner(store, hang)
    lim = _limiter(FakeClock(), concurrent=1)
    reservation = lim.reserve(None, slot=True)
    await runs_api._run_in_background(runner, "r1", _body(), reservation, 0.05)

    rec = store.get("r1")
    assert rec["status"] == "cancelled"
    assert not [e for e in rec["events"] if e["type"] == "run.failed"]
    assert lim.active == 0  # the slot frees when the task ends, not at cancel


async def test_cancel_does_not_free_the_slot_until_the_task_ends() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    release = asyncio.Event()

    async def wait(**_kw):
        await release.wait()

    runner = _StubRunner(store, wait)
    lim = _limiter(FakeClock(), concurrent=1)
    reservation = lim.reserve(None, slot=True)
    task = asyncio.create_task(
        runs_api._run_in_background(runner, "r1", _body(), reservation, 60)
    )
    await asyncio.sleep(0)
    store.upsert("r1", {"status": "cancelled"})  # what /cancel does
    await asyncio.sleep(0)
    assert lim.active == 1
    release.set()
    await task
    assert lim.active == 0
    assert store.get("r1")["status"] == "cancelled"


async def test_a_run_finishing_in_time_is_untouched_by_the_deadline() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "success", "events": []})

    async def quick(**_kw):
        return None

    runner = _StubRunner(store, quick)
    await runs_api._run_in_background(runner, "r1", _body(), None, 5)
    assert store.get("r1")["status"] == "success"
    assert runner.teardowns == []


async def test_task_cancellation_frees_the_slot() -> None:
    # The background task itself cancelled (shutdown): CancelledError is not
    # an Exception, and the slot must still come back.
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})

    async def hang(**_kw):
        await asyncio.sleep(3600)

    lim = _limiter(FakeClock(), concurrent=1)
    reservation = lim.reserve(None, slot=True)
    task = asyncio.create_task(
        runs_api._run_in_background(_StubRunner(store, hang), "r1", _body(), reservation, 60)
    )
    await asyncio.sleep(0)
    assert lim.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lim.active == 0


async def test_deadline_finalize_store_failure_still_frees_and_tears_down() -> None:
    class BrokenStore(RunStore):
        def upsert_unless_cancelled(self, *a, **k):
            raise RuntimeError("disk full")

    store = BrokenStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})

    async def hang(**_kw):
        await asyncio.sleep(3600)

    runner = _StubRunner(store, hang)
    lim = _limiter(FakeClock(), concurrent=1)
    reservation = lim.reserve(None, slot=True)
    await runs_api._run_in_background(runner, "r1", _body(), reservation, 0.05)
    assert lim.active == 0
    assert runner.teardowns == ["r1"]


async def test_deadline_through_the_real_scenario_runner() -> None:
    """The real engine, with agent spawn hung: one run.failed, cleanup once."""
    app = create_app(Settings())
    runner = app.state.runner
    calls = {"cleanup": 0, "teardown": 0}
    orig_cleanup = runner._cleanup_run

    def cleanup(*a, **k):
        calls["cleanup"] += 1
        return orig_cleanup(*a, **k)

    def teardown(run_id, **_k):
        calls["teardown"] += 1

    async def hang(**_kw):
        await asyncio.sleep(3600)

    runner._cleanup_run = cleanup
    runner.schedule_webhook_teardown = teardown
    runner.supervisor.launch = hang
    runner.store.upsert("rr", {"run_id": "rr", "status": "pending", "events": []})
    lim = _limiter(FakeClock(), concurrent=1)
    reservation = lim.reserve(None, slot=True)
    await runs_api._run_in_background(runner, "rr", _body(), reservation, 0.5)

    rec = runner.store.get("rr")
    assert rec["status"] == "failed"
    assert rec["error"].startswith("run_deadline_exceeded")
    assert [e["type"] for e in rec["events"]].count("run.failed") == 1
    assert calls == {"cleanup": 1, "teardown": 1}
    assert lim.active == 0
