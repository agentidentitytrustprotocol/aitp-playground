"""Hands out monotonic ports starting at a configurable base, with a recycle
pool for ports released by finished runs so long-running services don't drift
out of the allocator range.

Callers can request a specific offset from the base (e.g. for scenarios that
embed port literals in did:web hosts). If the requested offset collides with
an already-allocated port the allocator transparently falls back to the next
monotonic slot."""
from __future__ import annotations

import threading


class PortAllocator:
    def __init__(self, start: int = 8100) -> None:
        self._lock = threading.Lock()
        self._next = start
        self._start = start
        self._free: list[int] = []
        self._in_use: set[int] = set()

    def allocate(self, offset: int = 0) -> int:
        with self._lock:
            candidate = self._start + offset
            if candidate not in self._in_use and candidate not in self._free:
                self._in_use.add(candidate)
                if candidate >= self._next:
                    self._next = candidate + 1
                return candidate
            if self._free:
                p = self._free.pop()
                self._in_use.add(p)
                return p
            while self._next in self._in_use:
                self._next += 1
            p = self._next
            self._next += 1
            self._in_use.add(p)
            return p

    def release(self, port: int) -> None:
        with self._lock:
            if port in self._in_use:
                self._in_use.discard(port)
                self._free.append(port)

    def reset(self) -> None:
        with self._lock:
            self._next = self._start
            self._free.clear()
            self._in_use.clear()
