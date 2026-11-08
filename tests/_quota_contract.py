"""The behaviour every LLM quota store must share.

Run against MemoryQuotaStore and a fake-pool PostgresQuotaStore in
tests/unit/test_quota_store.py, and against a real Postgres in
tests/integration/test_quota_postgres.py. Each case takes a ``make`` factory:
``await make(limits, clock) -> QuotaStore`` returning a store with no usage.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from aitp_playground.quota import QuotaLimits

T0 = datetime(2026, 11, 7, 21, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def limits(ip_day=1000, ip_month=1000, g_day=1000, g_month=1000) -> QuotaLimits:
    return QuotaLimits(
        per_ip_per_day=ip_day, per_ip_per_month=ip_month,
        global_per_day=g_day, global_per_month=g_month,
    )


async def case_allows_up_to_limit_then_denies(make):
    store = await make(limits(g_day=3), Clock())
    for _ in range(3):
        assert (await store.charge(None)).allowed
    d = await store.charge(None)
    assert not d.allowed
    assert d.reason == "quota_exceeded"
    assert d.quota == "global_daily"
    assert d.limit == 3
    assert d.resets_at == "2026-11-08T00:00:00Z"


async def case_denial_charges_nothing(make):
    # A's third call is refused by its per-IP counter; it must not have used
    # up a global unit, so B still gets the remaining three.
    store = await make(limits(ip_day=2, g_day=5), Clock())
    assert (await store.charge("198.51.100.1")).allowed
    assert (await store.charge("198.51.100.1")).allowed
    d = await store.charge("198.51.100.1")
    assert (d.allowed, d.quota, d.limit) == (False, "per_ip_daily", 2)
    for _ in range(2):
        assert (await store.charge("198.51.100.2")).allowed
    assert (await store.charge("198.51.100.3")).allowed
    d = await store.charge("198.51.100.4")
    assert (d.allowed, d.quota) == (False, "global_daily")


async def case_n_crossing_limit_is_denied_whole(make):
    store = await make(limits(ip_day=5), Clock())
    assert (await store.charge("198.51.100.1", 4)).allowed
    d = await store.charge("198.51.100.1", 2)
    assert (d.allowed, d.quota) == (False, "per_ip_daily")
    # Nothing of the refused 2 was kept: one more unit still fits.
    assert (await store.charge("198.51.100.1", 1)).allowed
    assert not (await store.charge("198.51.100.1", 1)).allowed


async def case_no_subject_charges_global_only(make):
    store = await make(limits(ip_day=0, ip_month=0, g_day=2), Clock())
    assert (await store.charge(None)).allowed
    d = await store.charge("198.51.100.1")
    assert (d.allowed, d.quota) == (False, "per_ip_monthly")
    assert (await store.charge(None)).allowed
    assert not (await store.charge(None)).allowed


async def case_zero_limit_denies(make):
    store = await make(limits(g_day=0), Clock())
    d = await store.charge(None)
    assert (d.allowed, d.quota, d.limit) == (False, "global_daily", 0)


async def case_day_rollover(make):
    clock = Clock(datetime(2026, 11, 7, 23, 59, 59, tzinfo=UTC))
    store = await make(limits(ip_day=1, ip_month=3), clock)
    ip = "2001:db8:1:2::/64"
    assert (await store.charge(ip)).allowed
    assert not (await store.charge(ip)).allowed
    clock.now = datetime(2026, 11, 8, 0, 0, 0, tzinfo=UTC)
    assert (await store.charge(ip)).allowed  # new day
    clock.now += timedelta(days=1)
    assert (await store.charge(ip)).allowed
    clock.now += timedelta(days=1)
    d = await store.charge(ip)  # day fresh, month spent (3)
    assert (d.allowed, d.quota, d.limit) == (False, "per_ip_monthly", 3)
    assert d.resets_at == "2026-12-01T00:00:00Z"


async def case_month_rollover_including_year_end(make):
    clock = Clock(datetime(2026, 12, 31, 12, tzinfo=UTC))
    store = await make(limits(g_month=1), clock)
    assert (await store.charge(None)).allowed
    d = await store.charge(None)
    assert (d.quota, d.resets_at) == ("global_monthly", "2027-01-01T00:00:00Z")
    clock.now = datetime(2027, 1, 1, tzinfo=UTC)
    assert (await store.charge(None)).allowed


async def case_denial_quotes_latest_reset(make):
    # Day and month both spent: the month reset is when a call can succeed.
    store = await make(limits(g_day=1, g_month=1), Clock())
    assert (await store.charge(None)).allowed
    d = await store.charge(None)
    assert (d.quota, d.limit, d.resets_at) == ("global_monthly", 1, "2026-12-01T00:00:00Z")


async def case_concurrent_charges_never_exceed(make):
    store = await make(limits(ip_day=30), Clock())
    results = await asyncio.gather(*(store.charge("198.51.100.9") for _ in range(50)))
    assert sum(d.allowed for d in results) == 30
    assert all(d.quota == "per_ip_daily" for d in results if not d.allowed)


async def case_n_below_one_is_refused(make):
    store = await make(limits(), Clock())
    with pytest.raises(ValueError):
        await store.charge(None, 0)


CASES = [
    case_allows_up_to_limit_then_denies,
    case_denial_charges_nothing,
    case_n_crossing_limit_is_denied_whole,
    case_no_subject_charges_global_only,
    case_zero_limit_denies,
    case_day_rollover,
    case_month_rollover_including_year_end,
    case_denial_quotes_latest_reset,
    case_concurrent_charges_never_exceed,
    case_n_below_one_is_refused,
]
