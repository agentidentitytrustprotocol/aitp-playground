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


def test_hydrate_skips_a_row_with_unreadable_json_and_keeps_the_rest(tmp_path: Path) -> None:
    """A corrupted `record` blob (partial write, disk issue) must not take
    down hydration for every other run — it's logged and skipped."""
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("good", {"run_id": "good", "status": "running", "events": []})
    store.close()

    raw = sqlite3.connect(str(db))
    raw.execute(
        "INSERT INTO runs(run_id, status, scenario_ref, created_at, record) "
        "VALUES (?,?,?,?,?)",
        ("bad", "running", None, 0.0, "{not valid json"),
    )
    raw.commit()
    raw.close()

    reopened = SqliteRunStore(str(db))
    assert reopened.list_ids() == ["good"]
    assert reopened.get("bad") is None
    reopened.close()


def test_persist_record_is_a_noop_for_a_run_id_never_upserted(tmp_path: Path) -> None:
    """Defensive branch: `_persist_record` is only ever called (by `upsert`/
    `append_event`) after the in-memory record already exists, but it must
    still no-op safely rather than write a garbage row if that ever changes."""
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store._persist_record("never-upserted")

    raw = sqlite3.connect(str(db))
    rows = raw.execute("SELECT * FROM runs").fetchall()
    raw.close()
    assert rows == []
    store.close()


def test_a_set_value_in_a_record_round_trips_via_json_default(tmp_path: Path) -> None:
    """Exercises `_json_default`'s set/frozenset branch through the real
    persistence path, not just as a pure function."""
    db = tmp_path / "runs.sqlite"
    store = SqliteRunStore(str(db))
    store.upsert("r1", {
        "run_id": "r1", "status": "running", "events": [], "tags": {"b", "a"},
    })
    store.close()

    reopened = SqliteRunStore(str(db))
    assert reopened.get("r1")["tags"] == ["a", "b"]
    reopened.close()
