"""Tests for the SQLite-backed RunStore (RUN_HISTORY_DB)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from aitp_playground.runner.store import (
    RunStore,
    SqliteRunStore,
    build_run_store,
)


def test_build_run_store_returns_in_memory_when_unset(tmp_path: Path) -> None:
    store = build_run_store(None)
    assert type(store) is RunStore
    store2 = build_run_store("")
    assert type(store2) is RunStore


def test_build_run_store_returns_sqlite_when_set(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store = build_run_store(str(db))
    assert isinstance(store, SqliteRunStore)
    assert db.exists()
    store.close()


def test_upsert_and_get_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("r1", {
        "run_id": "r1", "status": "running",
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "outputs": {}, "events": [], "error": None,
    })
    rec = store.get("r1")
    assert rec is not None
    assert rec["status"] == "running"
    assert rec["scenario_ref"] == "intra-org/research-and-write@1.0.0"
    store.close()


def test_persisted_runs_survive_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("r1", {
        "run_id": "r1", "status": "running",
        "scenario_ref": "intra-org/research-and-write@1.0.0",
        "outputs": {}, "events": [], "error": None,
    })
    store.append_event("r1", {"type": "run.started", "scenario_ref": "x"})
    store.append_event("r1", {"type": "handshake.completed", "agent_id": "a"})
    store.upsert("r1", {"status": "success", "outputs": {"final": "ok"}})
    store.close()

    # Simulate a process restart by opening a fresh store on the same DB.
    reopened = SqliteRunStore(str(db))
    rec = reopened.get("r1")
    assert rec is not None
    assert rec["status"] == "success"
    assert rec["outputs"] == {"final": "ok"}
    assert [e["type"] for e in rec["events"]] == [
        "run.started", "handshake.completed",
    ]
    assert reopened.list_ids() == ["r1"]
    reopened.close()


def test_events_are_appended_with_monotonic_seq(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    for i in range(5):
        store.append_event("r1", {"type": "step", "i": i})
    store.close()

    # Inspect the raw rows to confirm seq is monotonic per run.
    raw = sqlite3.connect(str(db))
    rows = raw.execute(
        "SELECT seq FROM run_events WHERE run_id=? ORDER BY seq", ("r1",),
    ).fetchall()
    raw.close()
    assert [r[0] for r in rows] == [0, 1, 2, 3, 4]


def test_status_update_overwrites_scalar_row(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    store.upsert("r1", {"status": "cancelled"})
    store.close()

    raw = sqlite3.connect(str(db))
    rows = raw.execute(
        "SELECT status FROM runs WHERE run_id=?", ("r1",),
    ).fetchall()
    raw.close()
    # One row, current status reflects the latest upsert.
    assert rows == [("cancelled",)]


def test_isolated_runs_do_not_cross_contaminate(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    store.upsert("r2", {"run_id": "r2", "status": "running", "events": []})
    store.append_event("r1", {"type": "a"})
    store.append_event("r2", {"type": "b"})
    store.append_event("r2", {"type": "c"})
    store.close()

    reopened = SqliteRunStore(str(db))
    assert [e["type"] for e in reopened.get("r1")["events"]] == ["a"]
    assert [e["type"] for e in reopened.get("r2")["events"]] == ["b", "c"]
    reopened.close()


def test_db_parent_directory_is_created(tmp_path: Path) -> None:
    """Path with a missing parent dir should be created — keeps env-var
    config ergonomic (just point at a path)."""
    db = tmp_path / "nested" / "subdir" / "runs.sqlite"
    store = SqliteRunStore(str(db))
    assert db.exists()
    store.close()
