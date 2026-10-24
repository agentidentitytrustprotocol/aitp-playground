"""In-process rate limits and the global spend cap.

Two primitives, both deliberately in process memory (no Redis): a token
bucket keyed by an arbitrary string (client IP, API-key fingerprint), and a
rolling-window counter for the global ``MAX_RUNS_PER_HOUR`` cap.

SINGLE-REPLICA ASSUMPTION: every limit here is per process. The service is
deployed as one Railway replica; running N replicas multiplies each limit by
N. Scaling out means moving this state to a shared store first. SECURITY.md
records the same assumption.

Both take an injectable ``clock`` so tests can drive time without sleeping.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from .config import Settings

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
