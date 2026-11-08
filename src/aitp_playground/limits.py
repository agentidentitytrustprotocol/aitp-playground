"""In-process rate limits, the global spend cap and the run launch limits.

All deliberately in process memory (no Redis): a token bucket keyed by an
arbitrary string (client IP, API-key fingerprint), a rolling-window counter
for the global ``MAX_RUNS_PER_HOUR`` cap, and :class:`RunLimiter` (a per-IP
sliding-window log of launches plus a service-wide concurrency count).

SINGLE-REPLICA ASSUMPTION: every limit here is per process. The service is
deployed as one Railway replica; running N replicas multiplies each limit by
N. Scaling out means moving this state to a shared store first. SECURITY.md
records the same assumption.

Both take an injectable ``clock`` so tests can drive time without sleeping.
"""
from __future__ import annotations

import itertools
import math
import threading
import time
from collections import deque
from typing import Callable

from .config import Settings
from .errors import RunLimitExceeded

Clock = Callable[[], float]

#: Above this many tracked keys, buckets that have refilled completely are
#: dropped — a full bucket is indistinguishable from a fresh one, so pruning
#: it loses nothing and keeps a key-spraying caller from growing memory.
_PRUNE_ABOVE = 10_000


class TokenBucket:
    """``burst`` tokens per key, refilled at ``rate_per_sec``."""

    def __init__(self, *, rate_per_sec: float, burst: int, clock: Clock = time.monotonic) -> None:
        self.rate = rate_per_sec
        self.burst = max(1, burst)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last)

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            if len(self._buckets) > _PRUNE_ABOVE:
                self._prune(now)
            return allowed

    def _prune(self, now: float) -> None:
        full = [
            k for k, (t, last) in self._buckets.items()
            if t + (now - last) * self.rate >= self.burst
        ]
        for k in full:
            del self._buckets[k]


class RollingCap:
    """At most ``limit`` acquisitions in any rolling ``window_secs``."""

    def __init__(self, *, limit: int, window_secs: float = 3600.0, clock: Clock = time.monotonic) -> None:
        self.limit = limit
        self.window = window_secs
        self._clock = clock
        self._lock = threading.Lock()
        self._stamps: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._stamps and now - self._stamps[0] >= self.window:
                self._stamps.popleft()
            if len(self._stamps) >= self.limit:
                return False
            self._stamps.append(now)
            return True


class SpendLimits:
    """The limits applied to LLM-spending routes (run create, invoke)."""

    def __init__(self, settings: Settings, clock: Clock = time.monotonic) -> None:
        rate = settings.rate_limit_per_minute / 60.0
        self.per_ip = TokenBucket(rate_per_sec=rate, burst=settings.rate_limit_burst, clock=clock)
        self.per_key = TokenBucket(rate_per_sec=rate, burst=settings.rate_limit_burst, clock=clock)
        self.hourly = RollingCap(limit=settings.max_runs_per_hour, clock=clock)


#: Retry-After for a refused launch at the concurrency cap: runs take minutes,
#: there is no single "next free" instant to promise, so ask for a short poll.
CONCURRENCY_RETRY_AFTER = 10

#: Above this many tracked IPs, windows with no live launch are dropped.
_PRUNE_WINDOWS_ABOVE = 4096


class Reservation:
    """One admitted launch: a concurrency slot (optional) and a window stamp.

    ``release()`` frees the slot and is idempotent; ``cancel()`` also takes
    the window stamp back, for a launch refused or abandoned before a run was
    created. ``handed_off`` is set once a background run owns the slot, so the
    request's own cleanup leaves it alone.
    """

    __slots__ = ("_limiter", "_slot_id", "_ip", "_stamp", "handed_off")

    def __init__(
        self, limiter: RunLimiter, slot_id: int | None, ip: str | None, stamp: float | None,
    ) -> None:
        self._limiter = limiter
        self._slot_id = slot_id
        self._ip = ip
        self._stamp = stamp
        self.handed_off = False

    @property
    def holds_slot(self) -> bool:
        return self._slot_id is not None

    def release(self) -> None:
        slot_id, self._slot_id = self._slot_id, None
        if slot_id is not None:
            self._limiter._release_slot(slot_id)

    def cancel(self) -> None:
        self.release()
        ip, stamp = self._ip, self._stamp
        self._ip = self._stamp = None
        if ip is not None and stamp is not None:
            self._limiter._unstamp(ip, stamp)


class RunLimiter:
    """Per-IP launch window plus the service-wide concurrency cap.

    Check-and-reserve happens under one lock, so concurrent launches can never
    both take the last slot or the last window entry. A slot is held until the
    run's background task ends and calls :meth:`Reservation.release`; as a
    backstop against a leak (a background task that never started), a slot
    older than ``max_hold_secs`` is reclaimed on the next reservation.
    """

    def __init__(
        self,
        *,
        per_ip: int,
        window_secs: float,
        max_concurrent: int,
        max_hold_secs: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.per_ip = per_ip
        self.window = window_secs
        self.max_concurrent = max_concurrent
        self.max_hold = max_hold_secs
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}
        self._slots: dict[int, float] = {}  # slot id -> acquired at
        self._ids = itertools.count(1)

    @classmethod
    def from_settings(cls, settings: Settings, clock: Clock = time.monotonic) -> RunLimiter:
        return cls(
            per_ip=settings.run_rate_limit_per_ip,
            window_secs=settings.run_rate_limit_window_seconds,
            max_concurrent=settings.max_concurrent_runs,
            # The deadline ends a run; the grace covers its teardown.
            max_hold_secs=settings.run_deadline_seconds + 120.0,
            clock=clock,
        )

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._slots)

    def reserve(self, ip: str | None, *, slot: bool) -> Reservation:
        """Admit one launch or raise :class:`RunLimitExceeded`.

        ``ip`` is the trusted subject IP, or ``None`` (no per-IP window).
        The concurrency cap is checked first, so a launch refused for
        concurrency never uses up a window entry.
        """
        now = self._clock()
        with self._lock:
            if slot:
                self._reclaim_stale(now)
                if len(self._slots) >= self.max_concurrent:
                    raise RunLimitExceeded(
                        "max_concurrent_runs",
                        limit=self.max_concurrent,
                        retry_after=CONCURRENCY_RETRY_AFTER,
                        detail=f"{self.max_concurrent} runs are already in progress; "
                        "try again shortly",
                    )
            stamp: float | None = None
            if ip is not None:
                stamps = self._windows.setdefault(ip, deque())
                self._expire(stamps, now)
                if len(stamps) >= self.per_ip:
                    retry = math.ceil(stamps[0] + self.window - now)
                    raise RunLimitExceeded(
                        "run_rate_limited",
                        limit=self.per_ip,
                        retry_after=max(1, retry),
                        detail=f"at most {self.per_ip} run launches per "
                        f"{int(self.window)}s from one client IP",
                    )
                stamps.append(now)
                stamp = now
                if len(self._windows) > _PRUNE_WINDOWS_ABOVE:
                    self._prune_windows(now)
            slot_id: int | None = None
            if slot:
                slot_id = next(self._ids)
                self._slots[slot_id] = now
        return Reservation(self, slot_id, ip, stamp)

    def _expire(self, stamps: deque[float], now: float) -> None:
        while stamps and now - stamps[0] >= self.window:
            stamps.popleft()

    def _prune_windows(self, now: float) -> None:
        for ip in list(self._windows):
            stamps = self._windows[ip]
            self._expire(stamps, now)
            if not stamps:
                del self._windows[ip]

    def _reclaim_stale(self, now: float) -> None:
        for slot_id, since in list(self._slots.items()):
            if now - since >= self.max_hold:
                del self._slots[slot_id]

    def _release_slot(self, slot_id: int) -> None:
        with self._lock:
            self._slots.pop(slot_id, None)

    def _unstamp(self, ip: str, stamp: float) -> None:
        with self._lock:
            stamps = self._windows.get(ip)
            if stamps is None:
                return
            try:
                stamps.remove(stamp)
            except ValueError:
                pass
            if not stamps:
                del self._windows[ip]
