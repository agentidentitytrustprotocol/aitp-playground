"""In-memory run store with per-run event pub/sub."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Optional


class RunStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        # SSE subscribers per run_id. Queues live on the event loop that
        # subscribed, so we MUST use the loop reference captured at subscribe
        # time when delivering from another thread.
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]] = {}

    def upsert(self, run_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            existing = self._runs.get(run_id, {})
            if "created_at" not in existing and "created_at" not in record:
                record = {**record, "created_at": time.time()}
            merged = {**existing, **record}
            merged["events"] = list(existing.get("events", [])) + list(record.get("events", []))
            self._runs[run_id] = merged

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            record = self._runs.setdefault(
                run_id, {"run_id": run_id, "status": "running", "events": [], "created_at": time.time()},
            )
            record.setdefault("events", []).append(event)
            subs = list(self._subscribers.get(run_id, []))
        # Notify outside the lock. Always use call_soon_threadsafe in case the
        # producer runs on a different thread/loop than the subscriber.
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_safe_put, q, event)
            except RuntimeError:
                # Loop is closed — subscriber went away; drop silently.
                pass

    def subscribe(self, run_id: str) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Return (queue, backlog). Queue receives events emitted *after* this
        call; backlog is everything emitted before."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        loop = asyncio.get_event_loop()
        with self._lock:
            backlog = list(self._runs.get(run_id, {}).get("events", []))
            self._subscribers.setdefault(run_id, []).append((loop, q))
        return q, backlog

    def unsubscribe(self, run_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            subs = self._subscribers.get(run_id, [])
            self._subscribers[run_id] = [(l, qq) for (l, qq) in subs if qq is not q]

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._runs.get(run_id)

    def list_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs.keys())


def _safe_put(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        # Slow consumer — drop. They can backfill via GET /runs/:id.
        pass
