"""Each agent's ``do_*`` handler and the LLM quota permit.

* no real path (no provider key or library): the quota is never asked and
  no mock note is shown, because nothing would have been charged;
* real path, permit refused (quota denial or a failed check): the builder
  runs on its stub, the model is never reached, and ``llm.started`` /
  ``llm.complete`` carry the mock note;
* real path, permit granted: the real builder runs once, through
  ``call_llm_or_502``, with no note.

The ``main`` modules are imported for real from a generated bootstrap file;
only the permit, the builders and the event sink are swapped out.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_llm_response_text import agent_libs  # noqa: F401 - fixture

AGENTS = Path(__file__).resolve().parents[2] / "agents"


def _bootstrap(agent_id: str, caps: list[str]) -> dict[str, Any]:
    return {
        "run_id": "run-permit", "agent_id": agent_id, "port": 0, "listen_host": "127.0.0.1",
        "inputs": {"style": "casual"},
        "playground": {"quota_url": "http://127.0.0.1:1/internal/llm-quota",
                       "telemetry_token": "t"},
        "aitp": {
            "seed_hex": "42" * 32, "display_name": agent_id,
            "handshake_endpoint": "https://localhost:8100/aitp/handshake/hello",
            "offered_caps": caps,
        },
    }


@pytest.fixture
def load_main(monkeypatch, tmp_path, agent_libs):  # noqa: F811
    """Import ``<agent>.main`` fresh against a bootstrap file."""
    monkeypatch.syspath_prepend(str(AGENTS))

    def load(agent: str, caps: list[str]):
        path = tmp_path / f"{agent}.json"
        path.write_text(json.dumps(_bootstrap(agent, caps)))
        monkeypatch.setenv("AITP_BOOTSTRAP_FILE", str(path))
        for name in [m for m in sys.modules if m == agent or m.startswith(f"{agent}.")]:
            monkeypatch.delitem(sys.modules, name)
        return importlib.import_module(f"{agent}.main")

    return load


class _Recorder:
    """Stands in for ``emit_event``, ``llm_permit`` and ``call_llm_or_502``."""

    def __init__(self, permit: bool):
        self.permit = permit
        self.events: list[tuple[str, dict]] = []
        self.permit_calls: list[dict] = []
        self.llm_calls: list[dict] = []

    async def emit(self, type_: str, _bs: dict, **fields: Any) -> None:
        self.events.append((type_, fields))

    async def llm_permit(self, _bs: dict, *, capability: str, calls: int, emit_event) -> bool:
        self.permit_calls.append({"capability": capability, "calls": calls})
        return self.permit

    async def call_llm(self, coro, **kwargs: Any):
        self.llm_calls.append(kwargs)
        return await coro

    def notes(self) -> list[Any]:
        return [f.get("notes") for t, f in self.events if t in ("llm.started", "llm.complete")]


def _wire(monkeypatch, main, rec: _Recorder, *, real: bool) -> None:
    monkeypatch.setattr(main, "emit_event", rec.emit)
    monkeypatch.setattr(main, "llm_permit", rec.llm_permit)
    monkeypatch.setattr(main, "call_llm_or_502", rec.call_llm)
    monkeypatch.setattr(main, "real_path_available", lambda: real)


# --- writer -------------------------------------------------------------------

def _writer(monkeypatch, load_main, *, real: bool, permit: bool):
    main = load_main("writer", ["write.content"])
    rec = _Recorder(permit)
    _wire(monkeypatch, main, rec, real=real)
    seen: list[bool] = []

    async def run_writer(findings, *, style, use_llm):
        seen.append(use_llm)
        return "REAL" if use_llm else "STUB"

    monkeypatch.setattr(main, "run_writer", run_writer)
    out = asyncio.run(main.do_write("findings"))
    return out, rec, seen


def test_writer_without_a_real_path_never_asks_the_quota(monkeypatch, load_main):
    out, rec, seen = _writer(monkeypatch, load_main, real=False, permit=True)
    assert out["article"] == "STUB" and seen == [False]
    assert rec.permit_calls == [] and rec.llm_calls == []
    assert rec.notes() == [None, None]


def test_writer_refused_runs_the_stub_with_the_mock_note(monkeypatch, load_main):
    out, rec, seen = _writer(monkeypatch, load_main, real=True, permit=False)
    assert out["article"] == "STUB" and seen == [False]
    assert rec.permit_calls == [{"capability": "write.content", "calls": 1}]
    assert rec.llm_calls == []
    assert rec.notes() == [rec_note(), rec_note()]


def test_writer_permitted_runs_the_model_once(monkeypatch, load_main):
    out, rec, seen = _writer(monkeypatch, load_main, real=True, permit=True)
    assert out["article"] == "REAL" and seen == [True]
    assert len(rec.llm_calls) == 1 and rec.llm_calls[0]["llm_calls"] == 1
    assert rec.notes() == [None, None]


# --- analyzer -----------------------------------------------------------------

def _analyzer(monkeypatch, load_main, *, real: bool, permit: bool):
    main = load_main("analyzer", ["analyze.data"])
    rec = _Recorder(permit)
    _wire(monkeypatch, main, rec, real=real)
    seen: list[bool] = []

    async def run_analyzer(text, *, use_llm):
        seen.append(use_llm)
        return {"summary": "REAL" if use_llm else "STUB"}

    monkeypatch.setattr(main, "run_analyzer", run_analyzer)
    out = asyncio.run(main.do_analyze("data"))
    return out, rec, seen


def test_analyzer_without_a_real_path_never_asks_the_quota(monkeypatch, load_main):
    out, rec, seen = _analyzer(monkeypatch, load_main, real=False, permit=True)
    assert out["summary"] == "STUB" and seen == [False]
    assert rec.permit_calls == [] and rec.notes() == [None, None]


def test_analyzer_refused_runs_the_stub_with_the_mock_note(monkeypatch, load_main):
    out, rec, seen = _analyzer(monkeypatch, load_main, real=True, permit=False)
    assert out["summary"] == "STUB" and seen == [False]
    assert rec.permit_calls == [{"capability": "analyze.data", "calls": 2}]
    assert rec.llm_calls == [] and rec.notes() == [rec_note(), rec_note()]


def test_analyzer_permitted_runs_the_model_once(monkeypatch, load_main):
    out, rec, seen = _analyzer(monkeypatch, load_main, real=True, permit=True)
    assert out["summary"] == "REAL" and seen == [True]
    assert [c["llm_calls"] for c in rec.llm_calls] == [2]
    assert rec.notes() == [None, None]


# --- researcher ---------------------------------------------------------------

class _RealCrew:
    async def kickoff_async(self):
        return "REAL"


class _StubCrew:
    def kickoff(self):
        return "STUB"


@pytest.mark.parametrize("handler,cap", [("do_research", "research.query"),
                                         ("do_deep_research", "research.deep")])
@pytest.mark.parametrize("real,permit,expected,asked,note", [
    (False, True, "STUB", False, False),
    (True, False, "STUB", True, True),
    (True, True, "REAL", True, False),
])
def test_researcher_permit_branching(monkeypatch, load_main, handler, cap,
                                     real, permit, expected, asked, note):
    main = load_main("researcher", ["research.query", "research.deep"])
    rec = _Recorder(permit)
    _wire(monkeypatch, main, rec, real=real)
    seen: list[bool] = []

    def build_crew(inputs, *, use_llm):
        seen.append(use_llm)
        return _RealCrew() if use_llm else _StubCrew()

    monkeypatch.setattr(main, "build_crew", build_crew)
    out = asyncio.run(getattr(main, handler)("topic"))
    assert expected in json.dumps(out)
    assert seen == [expected == "REAL"]
    assert rec.permit_calls == ([{"capability": cap, "calls": main.RESEARCH_LLM_CALLS}]
                                if asked else [])
    assert len(rec.llm_calls) == (1 if expected == "REAL" else 0)
    assert rec.notes() == [rec_note() if note else None] * 2


def rec_note() -> str:
    import llm
    return llm.MOCK_NOTE
