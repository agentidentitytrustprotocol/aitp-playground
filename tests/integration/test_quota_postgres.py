"""PostgresQuotaStore against a real Postgres.

Needs ``PLAYGROUND_TEST_DATABASE_URL`` (CI's integration job runs a
postgres:17 service and sets it); skipped without it. The database is
scratch: every test truncates ``playground_llm_usage``.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

asyncpg = pytest.importorskip("asyncpg")

from aitp_playground.quota import PostgresQuotaStore
from tests._quota_contract import CASES, Clock, limits

DSN = os.environ.get("PLAYGROUND_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="PLAYGROUND_TEST_DATABASE_URL is not set")


async def _reset() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS playground_llm_usage")
    finally:
        await conn.close()


async def _rows() -> dict[tuple, int]:
    conn = await asyncpg.connect(DSN)
    try:
        recs = await conn.fetch("SELECT scope, subject, window_start, calls FROM playground_llm_usage")
    finally:
        await conn.close()
    return {(r["scope"], r["subject"], r["window_start"]): r["calls"] for r in recs}


@pytest.fixture
async def stores():
    await _reset()
    made: list[PostgresQuotaStore] = []

    async def make(lim, clock):
        store = PostgresQuotaStore(DSN, lim, clock=clock)
        made.append(store)
        return store

    yield make
    for s in made:
        await s.aclose()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
async def test_contract_on_real_postgres(stores, case):
    await case(stores)


async def test_rows_and_rollback(stores):
    store = await stores(limits(ip_day=2), Clock())
    assert (await store.charge("198.51.100.1", 2)).allowed
    assert not (await store.charge("198.51.100.1")).allowed  # rolled back
    rows = await _rows()
    assert rows == {
        ("global_day", "*", date(2026, 11, 7)): 2,
        ("global_month", "*", date(2026, 11, 1)): 2,
        ("ip_day", "198.51.100.1", date(2026, 11, 7)): 2,
        ("ip_month", "198.51.100.1", date(2026, 11, 1)): 2,
    }


async def test_two_stores_share_counts_like_two_replicas(stores):
    a = await stores(limits(g_day=10), Clock())
    b = await stores(limits(g_day=10), Clock())
    results = await asyncio.gather(*(s.charge(None) for s in [a, b] * 10), b.charge(None))
    assert sum(d.allowed for d in results) == 10


async def test_schema_is_idempotent_and_prunes_dead_rows(stores):
    first = await stores(limits(), Clock())
    assert (await first.charge(None)).allowed
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "INSERT INTO playground_llm_usage VALUES ('ip_day', 'old', '2026-01-01', 5)"
        )
    finally:
        await conn.close()
    second = await stores(limits(), Clock())
    assert (await second.charge(None)).allowed  # opens its own pool: schema + prune again
    rows = await _rows()
    assert ("ip_day", "old", date(2026, 1, 1)) not in rows
    assert rows[("global_day", "*", date(2026, 11, 7))] == 2


async def test_unreachable_database_denies():
    store = PostgresQuotaStore(
        "postgresql://nobody:pw@127.0.0.1:1/none", limits(), clock=Clock(), check_timeout=3.0,
    )
    d = await store.charge(None)
    assert (d.allowed, d.reason, d.limit, d.resets_at) == (
        False, "quota_store_unavailable", None, None)
    await store.aclose()
