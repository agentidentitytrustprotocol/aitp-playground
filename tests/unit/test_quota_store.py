"""LLM quota stores: the shared contract, failure paths and store selection.

Never skips. The Postgres store runs here against a fake pool that mimics
the one table (row upserts inside a transaction that rolls back on error);
tests/integration/test_quota_postgres.py runs the same contract on a real
Postgres.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from aitp_playground.config import Settings
from aitp_playground.quota import (
    MemoryQuotaStore,
    PostgresQuotaStore,
    QuotaLimits,
    UnavailableQuotaStore,
    counters_for,
    select_store,
)
from aitp_playground.quota.store import PRUNE, SCHEMA, UPSERT
from aitp_playground.quota.windows import prune_cutoff
from tests._quota_contract import CASES, Clock, limits

DSN = "postgresql://quota:s3cr3t-pw@db.invalid:5432/quota"


# --- a fake asyncpg pool ----------------------------------------------------

class FakeDb:
    """One table, serialised transactions (stands in for Postgres row locks)."""

    def __init__(self) -> None:
        self.rows: dict[tuple, int] = {}
        self.lock = asyncio.Lock()
        self.executed: list[tuple] = []
        self.fail_execute: BaseException | None = None
        self.fail_fetch: BaseException | None = None
        self.fetch_delay = 0.0


class FakeConn:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.staged: dict[tuple, int] | None = None

    async def execute(self, sql, *args):
        if self.db.fail_execute is not None:
            raise self.db.fail_execute
        self.db.executed.append((sql, args))
        if sql == PRUNE:
            for k in [k for k in self.db.rows if k[2] < args[0]]:
                del self.db.rows[k]

    async def fetchval(self, sql, scope, subject, window_start, n):
        assert sql == UPSERT and self.staged is not None
        if self.db.fetch_delay:
            await asyncio.sleep(self.db.fetch_delay)
        if self.db.fail_fetch is not None:
            raise self.db.fail_fetch
        key = (scope, subject, window_start)
        self.staged[key] = self.staged.get(key, self.db.rows.get(key, 0)) + n
        return self.staged[key]

    @asynccontextmanager
    async def transaction(self):
        async with self.db.lock:
            self.staged = {}
            try:
                yield
            except BaseException:
                self.staged = None  # rollback
                raise
            self.db.rows.update(self.staged)
            self.staged = None


class FakePool:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        assert not self.closed
        yield FakeConn(self.db)

    async def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True
        self.terminated = True


class Factory:
    def __init__(self, db: FakeDb | None = None) -> None:
        self.db = db or FakeDb()
        self.calls = 0
        self.pools: list[FakePool] = []
        self.fail: BaseException | None = None

    async def __call__(self, dsn: str):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        pool = FakePool(self.db)
        self.pools.append(pool)
        return pool


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def pg(lim: QuotaLimits, clock=None, factory=None, mono=None, **kw) -> PostgresQuotaStore:
    return PostgresQuotaStore(
        DSN, lim, clock=clock or Clock(), pool_factory=factory or Factory(),
        monotonic=mono or Mono(), **kw,
    )


# --- the contract, on every store -------------------------------------------

async def _make_memory(lim, clock):
    return MemoryQuotaStore(lim, clock=clock)


async def _make_fake_pg(lim, clock):
    return pg(lim, clock)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("make", [_make_memory, _make_fake_pg], ids=["memory", "postgres-fake"])
async def test_contract(make, case):
    await case(make)


# --- windows ----------------------------------------------------------------

def test_counters_fixed_order_and_windows():
    now = datetime(2026, 2, 28, 23, 0, tzinfo=UTC)
    cs = counters_for("198.51.100.1", limits(1, 2, 3, 4), now)
    assert [c.scope for c in cs] == ["global_day", "global_month", "ip_day", "ip_month"]
    assert [c.limit for c in cs] == [3, 4, 1, 2]
    assert cs[0].window_start == date(2026, 2, 28) and cs[1].window_start == date(2026, 2, 1)
    assert cs[0].resets_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert cs[1].resets_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert [c.subject for c in cs] == ["*", "*", "198.51.100.1", "198.51.100.1"]
    assert [c.scope for c in counters_for(None, limits(), now)] == ["global_day", "global_month"]


def test_counters_use_utc_not_local_time():
    from datetime import timedelta, timezone

    # 23:30 on Nov 7 at UTC-5 is 04:30 on Nov 8 UTC.
    now = datetime(2026, 11, 7, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert counters_for(None, limits(), now)[0].window_start == date(2026, 11, 8)
    with pytest.raises(ValueError):
        counters_for(None, limits(), datetime(2026, 11, 7))


def test_prune_cutoff_keeps_last_month():
    assert prune_cutoff(datetime(2026, 11, 7, tzinfo=UTC)) == date(2026, 8, 31)


async def test_memory_drops_dead_windows():
    clock = Clock(datetime(2026, 10, 31, tzinfo=UTC))
    store = MemoryQuotaStore(limits(), clock=clock)
    await store.charge("198.51.100.1")
    clock.now = datetime(2026, 11, 1, tzinfo=UTC)
    await store.charge("198.51.100.1")
    assert all(k[2] >= date(2026, 11, 1) for k in store._counts)
    assert store.usage("ip_month", "198.51.100.1", date(2026, 11, 1)) == 1


# --- unavailable ------------------------------------------------------------

async def test_unavailable_denies_always_with_null_limit():
    store = UnavailableQuotaStore(limits(g_day=10**9))
    for ip in (None, "198.51.100.1"):
        d = await store.charge(ip, 1)
        assert d.as_dict() == {
            "allowed": False, "reason": "quota_store_unavailable",
            "quota": "quota_store_unavailable", "limit": None, "resets_at": None,
        }


# --- postgres failure paths (fake pool) -------------------------------------

async def test_pg_init_creates_schema_and_prunes_once():
    f = Factory()
    store = pg(limits(), factory=f)
    await store.charge(None)
    await store.charge("198.51.100.1")
    assert f.calls == 1
    assert [sql for sql, _ in f.db.executed] == [SCHEMA, PRUNE]
    assert f.db.executed[1][1] == (date(2026, 8, 31),)
    assert store.healthy


async def test_pg_prune_removes_old_rows():
    db = FakeDb()
    db.rows[("ip_day", "x", date(2026, 1, 1))] = 3
    db.rows[("global_month", "*", date(2026, 10, 1))] = 3
    await pg(limits(), factory=Factory(db)).charge(None)
    assert ("ip_day", "x", date(2026, 1, 1)) not in db.rows
    assert db.rows[("global_month", "*", date(2026, 10, 1))] == 3


async def test_pg_connect_failure_denies_and_backs_off(caplog):
    f, mono = Factory(), Mono()
    f.fail = OSError("connection refused to " + DSN)
    store = pg(limits(), factory=f, mono=mono)
    caplog.set_level(logging.DEBUG)
    d = await store.charge(None)
    assert (d.allowed, d.reason, d.limit, d.resets_at) == (
        False, "quota_store_unavailable", None, None)
    mono.t += 29.9
    assert not (await store.charge(None)).allowed
    assert f.calls == 1  # backing off: no reconnect storm
    mono.t += 0.2
    f.fail = None
    assert (await store.charge(None)).allowed
    assert f.calls == 2
    assert "s3cr3t-pw" not in caplog.text and "db.invalid" not in caplog.text


async def test_pg_schema_failure_closes_pool_and_backs_off():
    f, mono = Factory(), Mono()
    f.db.fail_execute = RuntimeError("permission denied")
    store = pg(limits(), factory=f, mono=mono)
    assert not (await store.charge(None)).allowed
    assert f.pools[0].closed and getattr(f.pools[0], "terminated", False)
    assert not store.healthy
    assert not (await store.charge(None)).allowed
    assert f.calls == 1
    mono.t += 31
    f.db.fail_execute = None
    assert (await store.charge(None)).allowed
    assert not f.pools[1].closed


async def test_pg_query_failure_mid_run_denies_and_rolls_back():
    f = Factory()
    store = pg(limits(), factory=f)
    assert (await store.charge("198.51.100.1")).allowed
    before = dict(f.db.rows)
    f.db.fail_fetch = ConnectionResetError()
    d = await store.charge("198.51.100.1")
    assert (d.allowed, d.reason) == (False, "quota_store_unavailable")
    assert f.db.rows == before
    f.db.fail_fetch = None
    assert (await store.charge("198.51.100.1")).allowed


async def test_pg_slow_check_hits_deadline_and_denies():
    f = Factory()
    store = pg(limits(), factory=f, check_timeout=0.05)
    await store.start()
    f.db.fetch_delay = 1.0
    d = await store.charge(None)
    assert (d.allowed, d.reason) == (False, "quota_store_unavailable")
    f.db.fetch_delay = 0
    assert f.db.rows == {}  # the timed-out transaction committed nothing
    assert (await store.charge(None)).allowed


async def test_pg_slow_connect_times_out_then_backs_off():
    mono = Mono()

    async def hang(dsn):
        await asyncio.sleep(10)

    store = pg(limits(), factory=hang, mono=mono, check_timeout=0.05)
    await store.start()  # never raises, never blocks past the deadline
    assert not store.healthy
    assert store._failed_at == mono.t


async def test_pg_start_swallows_failure_and_aclose():
    f = Factory()
    f.fail = OSError()
    store = pg(limits(), factory=f)
    await store.start()
    assert not store.healthy
    f2 = Factory()
    store2 = pg(limits(), factory=f2)
    await store2.start()
    assert store2.healthy
    await store2.aclose()
    assert f2.pools[0].closed and not store2.healthy
    await store2.aclose()  # idempotent


async def test_pg_charge_after_aclose_denies_without_reopening():
    f = Factory()
    store = pg(limits(), factory=f)
    assert (await store.charge(None)).allowed
    await store.aclose()
    d = await store.charge(None)
    assert (d.allowed, d.reason) == (False, "quota_store_unavailable")
    assert f.calls == 1 and not store.healthy


async def test_pg_aclose_during_connect_terminates_the_new_pool():
    f = Factory()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def slow(dsn):
        entered.set()
        await gate.wait()
        return await f(dsn)

    store = pg(limits(), factory=slow)
    charge = asyncio.create_task(store.charge(None))
    # Wait until the connect is really in flight; how many loop turns that
    # takes differs between Python versions.
    await asyncio.wait_for(entered.wait(), 5)
    await store.aclose()
    gate.set()
    d = await charge
    assert (d.allowed, d.reason) == (False, "quota_store_unavailable")
    assert f.pools[0].terminated and not store.healthy


async def test_pg_aclose_terminates_a_pool_whose_close_hangs():
    f = Factory()
    store = pg(limits(), factory=f, check_timeout=0.05)
    assert (await store.charge(None)).allowed
    pool = f.pools[0]

    async def hang():
        await asyncio.sleep(10)

    pool.close = hang
    await store.aclose()
    assert pool.terminated


async def test_pg_concurrent_first_use_opens_one_pool():
    f = Factory()
    store = pg(limits(), factory=f)
    await asyncio.gather(*(store.charge(None) for _ in range(20)))
    assert f.calls == 1


# --- selection --------------------------------------------------------------

def _settings(**kw) -> Settings:
    return Settings(**kw)


@pytest.mark.parametrize(
    "kw, railway, kind",
    [
        ({"database_url": DSN}, False, "postgres"),
        ({"database_url": DSN}, True, "postgres"),
        ({}, False, "memory"),
        ({}, True, "unavailable"),
        ({"database_url": "   "}, True, "unavailable"),
        ({"llm_quota_store": "postgres"}, False, "unavailable"),
        ({"llm_quota_store": "memory", "database_url": DSN}, True, "memory"),
        ({"llm_quota_store": "unavailable", "database_url": DSN}, False, "unavailable"),
    ],
)
def test_select_store_matrix(kw, railway, kind):
    assert select_store(_settings(**kw), railway=railway).kind == kind


def test_select_store_reads_railway_env(monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc")
    assert select_store(_settings()).kind == "unavailable"
    monkeypatch.delenv("RAILWAY_SERVICE_ID")
    assert select_store(_settings()).kind == "memory"


def test_select_store_passes_limits_and_never_logs_dsn(caplog):
    caplog.set_level(logging.DEBUG)
    store = select_store(
        _settings(database_url=DSN, llm_quota_per_ip_per_day=7, llm_quota_global_per_month=9),
        railway=True,
    )
    assert store.limits == QuotaLimits(7, 300, 1000, 9)
    assert "s3cr3t-pw" not in caplog.text


def test_quota_settings_defaults_env_and_validation(monkeypatch):
    s = _settings()
    assert (s.llm_quota_per_ip_per_day, s.llm_quota_per_ip_per_month,
            s.llm_quota_global_per_day, s.llm_quota_global_per_month) == (30, 300, 1000, 10000)
    assert s.llm_quota_store == "auto" and s.database_url == ""
    monkeypatch.setenv("LLM_QUOTA_GLOBAL_PER_DAY", "0")
    monkeypatch.setenv("LLM_QUOTA_STORE", "memory")
    monkeypatch.setenv("DATABASE_URL", DSN)
    s = _settings()
    assert (s.llm_quota_global_per_day, s.llm_quota_store, s.database_url) == (0, "memory", DSN)
    assert "s3cr3t-pw" not in repr(s)
    with pytest.raises(ValidationError):
        _settings(llm_quota_per_ip_per_day=-1)
    with pytest.raises(ValidationError):
        _settings(llm_quota_store="redis")


def test_app_lifespan_starts_and_closes_store(monkeypatch):
    from fastapi.testclient import TestClient

    from aitp_playground import main

    events: list[str] = []

    class Spy(MemoryQuotaStore):
        async def start(self):
            events.append("start")

        async def aclose(self):
            events.append("close")

    monkeypatch.setattr(main, "select_store", lambda s: Spy(QuotaLimits.from_settings(s)))
    app = main.create_app(Settings())
    assert isinstance(app.state.quota_store, Spy)
    with TestClient(app):
        assert events == ["start"]
    assert events == ["start", "close"]
