"""LLM-call quota stores.

``charge(subject_ip, n)`` charges ``n`` call units against every applicable
counter (see :mod:`.windows`) and is all or nothing: if any counter would go
past its limit, none is charged and the charge is denied with that counter's
limit and reset time.

Three stores, chosen by :func:`select_store`:

- :class:`PostgresQuotaStore`: durable counters in the dedicated playground
  Postgres (``DATABASE_URL``), one transaction per charge;
- :class:`MemoryQuotaStore`: process memory, for local development and tests;
- :class:`UnavailableQuotaStore`: denies everything. Used on Railway when no
  database is configured, so a missing ``DATABASE_URL`` can never silently
  turn into "counted in memory, reset on every deploy".

Every failure denies (fail closed) with ``reason=quota_store_unavailable``
and a null limit and reset: real LLM spend is never allowed without a count.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from ..config import Settings, on_railway
from .windows import Counter, QuotaLimits, counters_for, prune_cutoff, rfc3339

log = logging.getLogger(__name__)

WallClock = Callable[[], datetime]
Monotonic = Callable[[], float]

REASON_EXCEEDED = "quota_exceeded"
REASON_UNAVAILABLE = "quota_store_unavailable"

#: Deadline for one charge, pool acquisition included.
CHECK_TIMEOUT_S = 5.0
#: Connect timeout for the pool (asyncpg's default is 60 s).
CONNECT_TIMEOUT_S = 5.0
#: After a failed pool init, deny without retrying for this long, so a down
#: database doesn't get a connection storm from every charge.
INIT_BACKOFF_S = 30.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None  # quota_exceeded | quota_store_unavailable
    quota: str | None = None  # the counter that refused, e.g. per_ip_daily
    limit: int | None = None
    resets_at: str | None = None  # RFC 3339, UTC

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "quota": self.quota,
            "limit": self.limit,
            "resets_at": self.resets_at,
        }


ALLOWED = Decision(allowed=True)
UNAVAILABLE = Decision(allowed=False, reason=REASON_UNAVAILABLE, quota=REASON_UNAVAILABLE)


def _deny(over: list[Counter]) -> Decision:
    """Deny naming the exceeded counter that resets LAST.

    That reset is the earliest moment the charge could succeed again; when a
    daily and a monthly counter are both exhausted, quoting the daily reset
    would promise something tomorrow can't keep. Ties keep the fixed order.
    """
    worst = max(over, key=lambda c: c.resets_at)
    return Decision(False, REASON_EXCEEDED, worst.quota, worst.limit, rfc3339(worst.resets_at))


class QuotaStore:
    """Base: computes the counters, delegates the atomic charge."""

    kind = "abstract"

    def __init__(self, limits: QuotaLimits, *, clock: WallClock = _utcnow) -> None:
        self.limits = limits
        self._clock = clock

    async def charge(self, subject_ip: str | None, n: int = 1) -> Decision:
        if n < 1:
            raise ValueError("n must be >= 1")
        counters = counters_for(subject_ip, self.limits, self._clock())
        return await self._charge(counters, n)

    async def _charge(self, counters: list[Counter], n: int) -> Decision:
        raise NotImplementedError

    async def start(self) -> None:
        """Warm up (open a pool). Never raises; a failure is reported by charges."""

    async def aclose(self) -> None:
        return None


class MemoryQuotaStore(QuotaStore):
    """In-process counters. Lost on restart; local development and tests only."""

    kind = "memory"

    def __init__(self, limits: QuotaLimits, *, clock: WallClock = _utcnow) -> None:
        super().__init__(limits, clock=clock)
        self._counts: dict[tuple, int] = {}
        self._lock = asyncio.Lock()

    async def _charge(self, counters: list[Counter], n: int) -> Decision:
        async with self._lock:
            over = [c for c in counters if self._counts.get(c.key, 0) + n > c.limit]
            if over:
                return _deny(over)
            for c in counters:
                self._counts[c.key] = self._counts.get(c.key, 0) + n
            self._prune(counters)
            return ALLOWED

    def _prune(self, counters: list[Counter]) -> None:
        # Drop windows that ended: anything older than the current day/month.
        live = {c.window_start for c in counters}
        oldest = min(live)
        dead = [k for k in self._counts if k[2] < oldest]
        for k in dead:
            del self._counts[k]

    def usage(self, scope: str, subject: str, window_start) -> int:
        return self._counts.get((scope, subject, window_start), 0)


class UnavailableQuotaStore(QuotaStore):
    """Denies every charge. Railway without a database, or an explicit choice."""

    kind = "unavailable"

    async def _charge(self, counters: list[Counter], n: int) -> Decision:
        return UNAVAILABLE


SCHEMA = """
CREATE TABLE IF NOT EXISTS playground_llm_usage (
    scope        text    NOT NULL,
    subject      text    NOT NULL,
    window_start date    NOT NULL,
    calls        integer NOT NULL,
    PRIMARY KEY (scope, subject, window_start)
)
"""
UPSERT = """
INSERT INTO playground_llm_usage (scope, subject, window_start, calls)
VALUES ($1, $2, $3, $4)
ON CONFLICT (scope, subject, window_start)
DO UPDATE SET calls = playground_llm_usage.calls + EXCLUDED.calls
RETURNING calls
"""
PRUNE = "DELETE FROM playground_llm_usage WHERE window_start < $1"


class _OverLimit(Exception):
    def __init__(self, over: list[Counter]) -> None:
        self.over = over


async def _default_pool_factory(dsn: str) -> Any:
    import asyncpg  # noqa: PLC0415 - only needed when DATABASE_URL is set

    return await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, timeout=CONNECT_TIMEOUT_S, command_timeout=CHECK_TIMEOUT_S,
    )


class PostgresQuotaStore(QuotaStore):
    """Durable counters in one table; each charge is one transaction.

    Every counter is upserted (``+n``, row-locked) in the fixed order from
    :func:`counters_for`, so concurrent charges serialise per row without
    deadlocking. If any count passes its limit the transaction rolls back,
    which un-charges the counters already bumped.

    The pool opens lazily (or in :meth:`start`). If opening it fails, charges
    are denied without retrying for :data:`INIT_BACKOFF_S`. If the schema step
    fails the half-open pool is terminated (never a graceful close: that can
    hang on an unresponsive database while the init lock is held). After
    :meth:`aclose` every charge is denied; a late charge never reopens a pool.
    The DSN is never logged.
    """

    kind = "postgres"

    def __init__(
        self,
        dsn: str,
        limits: QuotaLimits,
        *,
        clock: WallClock = _utcnow,
        monotonic: Monotonic = time.monotonic,
        pool_factory: Callable[[str], Any] = _default_pool_factory,
        check_timeout: float = CHECK_TIMEOUT_S,
        init_backoff: float = INIT_BACKOFF_S,
    ) -> None:
        super().__init__(limits, clock=clock)
        self._dsn = dsn
        self._monotonic = monotonic
        self._pool_factory = pool_factory
        self._check_timeout = check_timeout
        self._init_backoff = init_backoff
        self._pool: Any = None
        self._init_lock = asyncio.Lock()
        self._failed_at: float | None = None
        self._closed = False

    @property
    def healthy(self) -> bool:
        return self._pool is not None

    async def _get_pool(self) -> Any:
        if self._closed:
            raise ConnectionError("quota store is closed")
        if self._pool is not None:
            return self._pool
        async with self._init_lock:
            if self._closed:
                raise ConnectionError("quota store is closed")
            if self._pool is not None:
                return self._pool
            if (
                self._failed_at is not None
                and self._monotonic() - self._failed_at < self._init_backoff
            ):
                raise ConnectionError("quota store backing off after a failed connect")
            try:
                pool = await self._pool_factory(self._dsn)
            except BaseException as exc:  # a timeout cancels us mid-connect too
                self._failed_at = self._monotonic()
                log.warning("LLM quota store: connecting failed (%s)", type(exc).__name__)
                raise
            try:
                async with pool.acquire() as conn:
                    await conn.execute(SCHEMA)
                    await conn.execute(PRUNE, prune_cutoff(self._clock()))
            except BaseException as exc:
                self._failed_at = self._monotonic()
                log.warning("LLM quota store: schema setup failed (%s)", type(exc).__name__)
                pool.terminate()
                raise
            if self._closed:  # aclose() ran while we were connecting
                pool.terminate()
                raise ConnectionError("quota store is closed")
            self._failed_at = None
            self._pool = pool
            log.info("LLM quota store: connected")
            return pool

    async def start(self) -> None:
        try:
            await asyncio.wait_for(self._get_pool(), timeout=self._check_timeout)
        except Exception:  # noqa: BLE001 - reported per charge; never block startup
            pass

    async def _charge(self, counters: list[Counter], n: int) -> Decision:
        try:
            return await asyncio.wait_for(self._charge_once(counters, n), self._check_timeout)
        except _OverLimit as e:
            return _deny(e.over)
        except Exception as exc:  # noqa: BLE001 - fail closed on anything
            log.warning("LLM quota check failed, denying (%s)", type(exc).__name__)
            return UNAVAILABLE

    async def _charge_once(self, counters: list[Counter], n: int) -> Decision:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            over: list[Counter] = []
            for c in counters:
                calls = await conn.fetchval(UPSERT, c.scope, c.subject, c.window_start, n)
                if calls > c.limit:
                    over.append(c)
            if over:
                raise _OverLimit(over)  # rolls the transaction back
        return ALLOWED

    async def aclose(self) -> None:
        self._closed = True
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=self._check_timeout)
            except Exception as exc:  # noqa: BLE001 - shutting down; don't hang it
                log.warning("LLM quota store: close timed out (%s)", type(exc).__name__)
                pool.terminate()


def select_store(
    settings: Settings,
    *,
    railway: bool | None = None,
    clock: WallClock = _utcnow,
) -> QuotaStore:
    """The store ``LLM_QUOTA_STORE`` selects.

    ``auto``: postgres when ``DATABASE_URL`` is set; otherwise memory off
    Railway and **unavailable** (deny everything) on Railway.
    """
    limits = QuotaLimits.from_settings(settings)
    mode = settings.llm_quota_store
    url = settings.database_url.strip()
    on_rw = on_railway() if railway is None else railway
    if mode == "auto":
        mode = "postgres" if url else ("unavailable" if on_rw else "memory")
    if mode == "postgres":
        if not url:
            log.warning("LLM_QUOTA_STORE=postgres but DATABASE_URL is not set: denying all LLM calls")
            return UnavailableQuotaStore(limits, clock=clock)
        return PostgresQuotaStore(url, limits, clock=clock)
    if mode == "memory":
        if on_rw:
            log.warning("LLM quotas are counted in memory on Railway and reset on every deploy")
        else:
            log.info("LLM quotas are counted in memory (DATABASE_URL not set)")
        return MemoryQuotaStore(limits, clock=clock)
    log.warning("LLM quota store is unavailable: every real LLM call is refused")
    return UnavailableQuotaStore(limits, clock=clock)
