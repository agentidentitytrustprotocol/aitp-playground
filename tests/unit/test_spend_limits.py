"""Rate limit (token bucket), the global hourly cap, and their wiring on the
spend routes — driven by a fake clock, never by sleeping."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aitp_playground.config import Settings
from aitp_playground.limits import RollingCap, SpendLimits, TokenBucket
from aitp_playground.main import create_app
from tests._auth import AUTH


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_token_bucket_allows_burst_then_refills_at_rate() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_sec=1.0, burst=3, clock=clock)
    assert [b.allow("k") for _ in range(4)] == [True, True, True, False]
    clock.now += 1.0
    assert b.allow("k") is True
    assert b.allow("k") is False
    # Keys are independent.
    assert b.allow("other") is True


def test_rolling_cap_refuses_the_n_plus_first_and_recovers_after_the_window() -> None:
    clock = FakeClock()
    cap = RollingCap(limit=3, window_secs=3600, clock=clock)
    assert [cap.try_acquire() for _ in range(4)] == [True, True, True, False]
    clock.now += 3599
    assert cap.try_acquire() is False
    clock.now += 1
    assert cap.try_acquire() is True


def _app(clock: FakeClock, **overrides):
    settings = Settings(
        rate_limit_per_minute=overrides.pop("rate_limit_per_minute", 100000),
        rate_limit_burst=overrides.pop("rate_limit_burst", 100000),
        max_runs_per_hour=overrides.pop("max_runs_per_hour", 100000),
        **overrides,
    )
    app = create_app(settings)
    app.state.spend_limits = SpendLimits(settings, clock=clock)
    # Never actually run a scenario: the limits are what's under test.
    async def _noop(**_kwargs):
        return None
    app.state.runner.run = _noop
    return app


_RUN = {"scenario_ref": "intra-org/trust-gate@1.0.0", "inputs": {"topic": "t"}}


def test_the_n_plus_first_run_in_an_hour_is_429() -> None:
    clock = FakeClock()
    client = TestClient(_app(clock, max_runs_per_hour=3), headers=AUTH)
    codes = [client.post("/runs", json=_RUN).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]
    clock.now += 3600
    assert client.post("/runs", json=_RUN).status_code == 202


def test_per_ip_rate_limit_is_429_and_keyed_on_the_proxy_appended_ip() -> None:
    clock = FakeClock()
    client = TestClient(_app(clock, rate_limit_burst=2, rate_limit_per_minute=1000000), headers=AUTH)
    limits = client.app.state.spend_limits
    # Separate per-key bucket so only the per-IP one is exercised here.
    limits.per_key = TokenBucket(rate_per_sec=0, burst=100, clock=clock)
    ip_a = {"X-Forwarded-For": "6.6.6.6, 1.1.1.1"}
    # Spoofed left-hand entries change; the proxy-appended right-most does not.
    ip_a2 = {"X-Forwarded-For": "7.7.7.7, 1.1.1.1"}
    ip_b = {"X-Forwarded-For": "2.2.2.2"}
    limits.per_ip = TokenBucket(rate_per_sec=0, burst=2, clock=clock)
    assert client.post("/runs", json=_RUN, headers=ip_a).status_code == 202
    assert client.post("/runs", json=_RUN, headers=ip_a2).status_code == 202
    assert client.post("/runs", json=_RUN, headers=ip_a).status_code == 429
    assert client.post("/runs", json=_RUN, headers=ip_b).status_code == 202


def test_per_key_rate_limit_is_429() -> None:
    clock = FakeClock()
    client = TestClient(_app(clock), headers=AUTH)
    client.app.state.spend_limits.per_key = TokenBucket(rate_per_sec=0, burst=1, clock=clock)
    assert client.post("/runs", json=_RUN, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 202
    assert client.post("/runs", json=_RUN, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_a_rate_limited_request_does_not_consume_the_hourly_cap() -> None:
    clock = FakeClock()
    client = TestClient(_app(clock, max_runs_per_hour=1), headers=AUTH)
    limits = client.app.state.spend_limits
    limits.per_ip = TokenBucket(rate_per_sec=0, burst=0, clock=clock)  # burst floors at 1
    assert client.post("/runs", json=_RUN).status_code == 202
    assert client.post("/runs", json=_RUN).status_code == 429
    assert len(limits.hourly._stamps) == 1


def test_unauthenticated_requests_never_touch_the_limits() -> None:
    clock = FakeClock()
    app = _app(clock, max_runs_per_hour=1)
    bare = TestClient(app)
    for _ in range(5):
        assert bare.post("/runs", json=_RUN).status_code == 401
    assert TestClient(app, headers=AUTH).post("/runs", json=_RUN).status_code == 202


def test_hosted_invoke_counts_against_the_hourly_cap() -> None:
    clock = FakeClock()
    client = TestClient(_app(clock, max_runs_per_hour=1), headers=AUTH)
    assert client.post("/runs", json=_RUN).status_code == 202
    r = client.post("/hosted-agents/nope/invoke", json={"peer_port": 1, "capability": "c"})
    assert r.status_code == 429
