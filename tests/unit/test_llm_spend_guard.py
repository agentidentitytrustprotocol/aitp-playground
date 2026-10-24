"""agents/base/llm.py spend guard: the per-run token budget is enforced
before the provider call, every model is max_tokens-capped, and provider
error text never carries a key."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import llm


@pytest.fixture(autouse=True)
def _fresh_budget(monkeypatch):
    monkeypatch.setattr(llm, "_BUDGET", llm.TokenBudget())
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-THISISAFAKEKEY0123456789")


def _bootstrap(budget: int, per_call: int = 100) -> dict:
    return {"llm": {"token_budget": budget, "max_tokens_per_call": per_call}}


async def _noop_emit(*_a, **_k) -> None:
    return None


def test_budget_refuses_before_the_provider_call_starts() -> None:
    started: list[int] = []

    async def provider_call(i: int) -> str:
        started.append(i)
        return "ok"

    bs = _bootstrap(budget=250)

    async def go():
        await llm.call_llm_or_502(provider_call(1), emit_event=_noop_emit, bootstrap=bs, task="t")
        await llm.call_llm_or_502(provider_call(2), emit_event=_noop_emit, bootstrap=bs, task="t")
        with pytest.raises(HTTPException) as exc:
            await llm.call_llm_or_502(provider_call(3), emit_event=_noop_emit, bootstrap=bs, task="t")
        return exc.value

    err = asyncio.run(go())
    assert err.status_code == 429
    assert started == [1, 2]  # the third call never reached the provider


def test_multi_call_tasks_reserve_for_every_call() -> None:
    async def provider_call() -> str:
        return "ok"

    bs = _bootstrap(budget=300)

    async def go():
        await llm.call_llm_or_502(provider_call(), emit_event=_noop_emit, bootstrap=bs, task="t", llm_calls=2)
        with pytest.raises(HTTPException):
            await llm.call_llm_or_502(provider_call(), emit_event=_noop_emit, bootstrap=bs, task="t", llm_calls=2)

    asyncio.run(go())


def test_stub_calls_are_not_budgeted(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")

    async def stub() -> str:
        return "ok"

    async def go():
        for _ in range(5):
            await llm.call_llm_or_502(stub(), emit_event=_noop_emit, bootstrap=_bootstrap(1), task="t")

    asyncio.run(go())


def test_provider_error_text_is_redacted() -> None:
    events: list[dict] = []

    async def emit(event_type, _bs, **fields):
        events.append({"type": event_type, **fields})

    async def failing() -> str:
        raise RuntimeError(
            "Incorrect API key provided: sk-proj-THISISAFAKEKEY0123456789 "
            "(also sk-ant-api03-abcdefgh, header Bearer abc.def.ghijklmnop)"
        )

    async def go():
        with pytest.raises(HTTPException) as exc:
            await llm.call_llm_or_502(failing(), emit_event=emit, bootstrap=_bootstrap(10**6), task="t")
        return exc.value

    err = asyncio.run(go())
    assert err.status_code == 502
    for text in (err.detail, events[-1]["detail"]):
        assert "THISISAFAKEKEY" not in text
        assert "abcdefgh" not in text
        assert "ghijklmnop" not in text
        assert "[redacted]" in text


def test_max_tokens_per_call_reads_the_bootstrap_cap() -> None:
    assert llm.max_tokens_per_call(_bootstrap(1, per_call=77)) == 77
    assert llm.max_tokens_per_call({"llm": {}}) == llm._DEFAULT_MAX_TOKENS_PER_CALL
