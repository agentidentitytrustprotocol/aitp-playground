"""Unit tests for the in-memory RunStore pub/sub apparatus and its
deterministic state-transition behavior (runner/store.py).

DECISIONS.md D-16 documents an *accepted* race between a cancelled run's
subprocess-kill and the background run task's own store write — this file
deliberately does not attempt to reproduce or assert away that race. It
covers the store's own, non-racy merge/notify semantics: upsert merging,
event backlog + live delivery to subscribers, unsubscribe, and the two
"terminal state" transitions (success, cancelled) a run's record goes
through, verifying only what the store itself guarantees.

tests/unit/test_run_store_sqlite.py covers the SQLite-backed subclass.
"""
from __future__ import annotations

import asyncio

from aitp_playground.runner.store import RunStore, _json_default, _safe_put


# --------------------------------------------------------------------------- #
# upsert() merge semantics
# --------------------------------------------------------------------------- #


def test_upsert_sets_created_at_once_and_never_overwrites_it() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running"})
    first_created_at = store.get("r1")["created_at"]

    store.upsert("r1", {"status": "success"})
    assert store.get("r1")["created_at"] == first_created_at


def test_upsert_merges_fields_without_clobbering_untouched_ones() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "scenario_ref": "x/y@1.0.0"})
    store.upsert("r1", {"status": "success", "outputs": {"final": "ok"}})

    record = store.get("r1")
    assert record["status"] == "success"
    assert record["scenario_ref"] == "x/y@1.0.0"  # untouched by the 2nd upsert
    assert record["outputs"] == {"final": "ok"}


# --------------------------------------------------------------------------- #
# state transitions: a run that completes normally vs. one that's cancelled
# --------------------------------------------------------------------------- #


def test_state_transition_running_to_success_preserves_the_event_log() -> None:
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    store.append_event("r1", {"type": "run.started"})
    store.append_event("r1", {"type": "step.complete"})
    store.upsert("r1", {"status": "success", "outputs": {"final": "ok"}})

    record = store.get("r1")
    assert record["status"] == "success"
    assert record["outputs"] == {"final": "ok"}
    assert [e["type"] for e in record["events"]] == ["run.started", "step.complete"]


def test_state_transition_running_to_cancelled_preserves_the_event_log() -> None:
    """The deterministic half of D-16: the store faithfully holds whatever
    status it's told and keeps appending to the same event log regardless of
    which terminal status wins. *Which* status wins the race against a
    concurrent subprocess-kill is the cancel route's job (D-16), not the
    store's — not re-tested here."""
    store = RunStore()
    store.upsert("r1", {"run_id": "r1", "status": "running", "events": []})
    store.append_event("r1", {"type": "run.started"})
    store.upsert("r1", {"status": "cancelled"})
    store.append_event("r1", {"type": "run.cancelled"})

    record = store.get("r1")
    assert record["status"] == "cancelled"
    assert [e["type"] for e in record["events"]] == ["run.started", "run.cancelled"]


def test_append_event_creates_the_run_record_on_first_use() -> None:
    """A run whose first store write is an event (not an upsert) still gets a
    sane default record — the shape ScenarioRunner relies on mid-run."""
    store = RunStore()
    store.append_event("r1", {"type": "run.started"})

    record = store.get("r1")
    assert record["status"] == "running"
    assert record["run_id"] == "r1"
    assert [e["type"] for e in record["events"]] == ["run.started"]


def test_get_and_list_ids_reflect_only_known_runs() -> None:
    store = RunStore()
    assert store.get("ghost") is None
    assert store.list_ids() == []

    store.upsert("r1", {"run_id": "r1", "status": "running"})
    store.append_event("r2", {"type": "run.started"})
    assert set(store.list_ids()) == {"r1", "r2"}


# --------------------------------------------------------------------------- #
# subscribe / unsubscribe / delivery
# --------------------------------------------------------------------------- #


async def test_subscribe_returns_backlog_then_streams_subsequent_events() -> None:
    store = RunStore()
    store.append_event("r1", {"type": "run.started"})

    q, backlog = store.subscribe("r1")
    assert [e["type"] for e in backlog] == ["run.started"]

    store.append_event("r1", {"type": "step.complete"})
    delivered = await asyncio.wait_for(q.get(), timeout=1)
    assert delivered["type"] == "step.complete"


async def test_subscribe_backlog_is_empty_for_a_run_with_no_events_yet() -> None:
    store = RunStore()
    q, backlog = store.subscribe("never-seen")
    assert backlog == []
    assert q.empty()


async def test_multiple_subscribers_each_receive_the_same_event() -> None:
    store = RunStore()
    q1, _ = store.subscribe("r1")
    q2, _ = store.subscribe("r1")

    store.append_event("r1", {"type": "run.started"})

    e1 = await asyncio.wait_for(q1.get(), timeout=1)
    e2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert e1["type"] == e2["type"] == "run.started"


async def test_unsubscribe_stops_further_delivery_to_that_queue() -> None:
    store = RunStore()
    q, _ = store.subscribe("r1")
    store.unsubscribe("r1", q)

    store.append_event("r1", {"type": "run.started"})
    await asyncio.sleep(0)  # let any (mis-)scheduled callback run
    assert q.empty()


def test_append_event_drops_delivery_silently_when_a_subscribers_loop_is_closed() -> None:
    """A subscriber's asyncio loop can close out from under a live run (the
    HTTP connection that opened the SSE stream went away). Notification must
    not raise or block the producer — it just drops that one delivery."""
    store = RunStore()

    class _DeadLoop:
        def call_soon_threadsafe(self, *args, **kwargs) -> None:
            raise RuntimeError("Event loop is closed")

    store._subscribers["r1"] = [(_DeadLoop(), object())]  # type: ignore[list-item]

    # Must not raise, and the event still lands in the run's own event log.
    store.append_event("r1", {"type": "run.complete"})
    assert store.get("r1")["events"][-1]["type"] == "run.complete"


# --------------------------------------------------------------------------- #
# module-level helpers
# --------------------------------------------------------------------------- #


def test_safe_put_drops_silently_when_the_queue_is_full() -> None:
    q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=1)
    q.put_nowait({"type": "first"})

    # Must not raise even though there's no room — slow consumers backfill
    # via GET /runs/:id instead.
    _safe_put(q, {"type": "second"})

    assert q.qsize() == 1
    assert q.get_nowait() == {"type": "first"}


def test_safe_put_delivers_when_the_queue_has_room() -> None:
    q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=1)
    _safe_put(q, {"type": "only"})
    assert q.get_nowait() == {"type": "only"}


def test_json_default_sorts_sets_and_frozensets_for_stable_output() -> None:
    assert _json_default({3, 1, 2}) == [1, 2, 3]
    assert _json_default(frozenset({"b", "a"})) == ["a", "b"]


def test_json_default_stringifies_anything_else() -> None:
    class Thing:
        def __repr__(self) -> str:
            return "<thing>"

    assert _json_default(Thing()) == "<thing>"
