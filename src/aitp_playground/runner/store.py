"""In-memory run store with per-run event pub/sub.

`RunStore` is the default. `SqliteRunStore` is a drop-in subclass that
mirrors every write to a SQLite file so `docker compose restart` (or a
crash) does not lose run history. The in-memory cache is still the
authoritative read source for SSE pub/sub — SQLite is the durable
sidecar, not the live event bus.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
            self._subscribers[run_id] = [(lp, qq) for (lp, qq) in subs if qq is not q]

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


class SqliteRunStore(RunStore):
    """RunStore that mirrors every write to a SQLite file.

    The in-memory cache is rehydrated from SQLite at construction so the
    service can resume serving a run that started in a previous process.
    Subscribers, however, are *not* rehydrated — they are live event-loop
    queues that disappear with the process. New subscribers replay the
    persisted backlog via ``subscribe()`` exactly like in-memory mode.
    """

    _SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id        TEXT PRIMARY KEY,
            status        TEXT,
            scenario_ref  TEXT,
            created_at    REAL,
            record        TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS run_events (
            run_id  TEXT NOT NULL,
            seq     INTEGER NOT NULL,
            event   TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id)",
    )

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because requests are served from threads
        # different from the one that opened the connection. Writes are
        # serialized through ``_db_lock`` below.
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None,
        )
        self._db_lock = threading.Lock()
        self._init_schema()
        self._hydrate()

    def _init_schema(self) -> None:
        with self._db_lock:
            for stmt in self._SCHEMA:
                self._conn.execute(stmt)

    def _hydrate(self) -> None:
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT run_id, record FROM runs"
            ).fetchall()
        loaded = 0
        for run_id, record_json in rows:
            try:
                record = json.loads(record_json)
            except json.JSONDecodeError:
                logger.warning("skipping unreadable run row run_id=%s", run_id)
                continue
            with self._db_lock:
                event_rows = self._conn.execute(
                    "SELECT event FROM run_events WHERE run_id=? ORDER BY seq",
                    (run_id,),
                ).fetchall()
            record["events"] = [json.loads(e[0]) for e in event_rows]
            with self._lock:
                self._runs[run_id] = record
            loaded += 1
        if loaded:
            logger.info(
                "RunStore: hydrated %d run(s) from %s", loaded, self._db_path,
            )

    def _persist_record(self, run_id: str) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            # The events table holds the canonical event log; the record
            # blob is only the run scalars.
            scalars = {k: v for k, v in record.items() if k != "events"}
        payload = json.dumps(scalars, default=_json_default)
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs"
                "(run_id, status, scenario_ref, created_at, record) "
                "VALUES (?,?,?,?,?)",
                (
                    run_id,
                    scalars.get("status"),
                    scalars.get("scenario_ref"),
                    scalars.get("created_at"),
                    payload,
                ),
            )

    def upsert(self, run_id: str, record: dict[str, Any]) -> None:
        super().upsert(run_id, record)
        self._persist_record(run_id)

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        super().append_event(run_id, event)
        # super() has already merged the event into self._runs[run_id]["events"].
        # The new event's seq is len(events) - 1.
        with self._lock:
            events = self._runs.get(run_id, {}).get("events", [])
            seq = len(events) - 1
            run_exists = run_id in self._runs
        if not run_exists:
            return
        payload = json.dumps(event, default=_json_default)
        with self._db_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_events(run_id, seq, event) "
                "VALUES (?,?,?)",
                (run_id, seq, payload),
            )
        # Status/created_at on the run scalar row may have updated as a
        # side-effect (e.g. the very first append_event creates the run).
        self._persist_record(run_id)

    def close(self) -> None:
        with self._db_lock:
            self._conn.close()


def _json_default(obj: Any) -> Any:
    """Fall back for non-JSON-native types that may sneak into a record."""
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def build_run_store(db_path: str | None) -> RunStore:
    """Factory: return an in-memory RunStore by default, or a SQLite-backed
    one when ``db_path`` is set. The path is created on demand.
    """
    if db_path:
        return SqliteRunStore(db_path)
    return RunStore()
