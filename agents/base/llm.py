"""Shared LLM selector for agent workers.

Provider is chosen by the LLM_PROVIDER env var (default: ``openai``).
The required API key for the chosen provider must be present; if it isn't,
``select_provider()`` returns ``None`` and callers fall back to their stub.

Models are overridable per-provider:
    OPENAI_MODEL      (default: gpt-4o-mini)
    ANTHROPIC_MODEL   (default: claude-sonnet-4-6)

For LangChain-based agents (writer, analyzer), use ``build_chat_model()``.
For CrewAI (researcher), use ``build_crewai_llm()`` so the model string is
in the litellm ``provider/model`` form CrewAI expects.

Spend guard: every model is built with ``max_tokens`` capped at the
bootstrap's ``llm.max_tokens_per_call``, and ``call_llm_or_502`` reserves
``llm_calls * max_tokens_per_call`` output tokens against this process's
``llm.token_budget`` BEFORE the provider call starts — once the budget is
spent the call is refused with 429 and the provider is never contacted.
Provider error text is passed through ``redact_secrets`` before it reaches a
response, a log line or telemetry.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import threading
from typing import Any, Awaitable, Literal, Optional, TypeVar

from fastapi import HTTPException

Provider = Literal["openai", "anthropic"]

T = TypeVar("T")

_OPENAI_DEFAULT = "gpt-4o-mini"
_ANTHROPIC_DEFAULT = "claude-sonnet-4-6"

_DEFAULT_MAX_TOKENS_PER_CALL = 1024
_DEFAULT_TOKEN_BUDGET = 40000

#: Env vars whose values must never appear in an error we surface.
_SECRET_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PLAYGROUND_API_KEY", "CP_API_KEY")
#: Key-shaped substrings (OpenAI/Anthropic keys, bearer tokens), even ones
#: that are not this process's own — a provider may echo a partial key.
_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-*]{6,}|(?i:bearer)\s+[A-Za-z0-9._~+/=\-]{8,})"
)


def redact_secrets(text: str) -> str:
    """Strip provider keys and bearer tokens out of *text*."""
    for name in _SECRET_ENV_VARS:
        for value in (os.environ.get(name) or "").split(","):
            value = value.strip()
            if len(value) >= 8:
                text = text.replace(value, "[redacted]")
    return _SECRET_PATTERN.sub("[redacted]", text)


def _bootstrap_llm_block() -> dict[str, Any]:
    path = os.environ.get("AITP_BOOTSTRAP_FILE", "")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return dict(json.load(f).get("llm") or {})
    except Exception:  # noqa: BLE001 — fall back to the safe defaults
        return {}


def max_tokens_per_call(bootstrap: Optional[dict] = None) -> int:
    block = (bootstrap or {}).get("llm") if bootstrap else _bootstrap_llm_block()
    return int((block or {}).get("max_tokens_per_call") or _DEFAULT_MAX_TOKENS_PER_CALL)


class TokenBudget:
    """Output tokens this agent process may still reserve."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.spent = 0

    def try_reserve(self, tokens: int, budget: int) -> bool:
        with self._lock:
            if self.spent + tokens > budget:
                return False
            self.spent += tokens
            return True


_BUDGET = TokenBudget()


def select_provider() -> Optional[Provider]:
    """Return the active provider, or ``None`` if no key is available for it."""
    requested = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
    if requested == "openai" and os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if requested == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def model_name(provider: Provider) -> str:
    if provider == "openai":
        return os.environ.get("OPENAI_MODEL") or _OPENAI_DEFAULT
    return os.environ.get("ANTHROPIC_MODEL") or _ANTHROPIC_DEFAULT


def build_chat_model():
    """Construct a LangChain chat model for the active provider.

    Returns ``None`` if no provider is configured or the optional package
    for the requested provider isn't installed. Callers must handle ``None``
    by falling back to their deterministic stub.
    """
    provider = select_provider()
    if provider == "openai":
        try:
            from langchain_openai import ChatOpenAI  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        return ChatOpenAI(
            model=model_name("openai"), temperature=0.2, max_tokens=max_tokens_per_call(),
        )
    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        return ChatAnthropic(model=model_name("anthropic"), max_tokens=max_tokens_per_call())
    return None


def build_crewai_llm():
    """Construct a ``crewai.LLM`` for the active provider, or None.

    CrewAI uses litellm under the hood, so the model is passed in the
    ``provider/model`` form (e.g. ``openai/gpt-4o-mini``).
    """
    provider = select_provider()
    if provider is None:
        return None
    try:
        from crewai import LLM  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return LLM(model=f"{provider}/{model_name(provider)}", max_tokens=max_tokens_per_call())


async def call_llm_or_502(
    coro: Awaitable[T], *, emit_event: Any, bootstrap: dict, task: str,
    llm_calls: int = 1, **extra: Any,
) -> T:
    """Await *coro* — a real provider call not yet started — and turn any
    failure into a clean 502 carrying the provider's own (redacted) error
    text, instead of an opaque 500.

    Before awaiting, reserve ``llm_calls * max_tokens_per_call`` output
    tokens against this process's budget (``bootstrap["llm"]``). When the
    reservation does not fit, *coro* is closed unstarted and the call is
    refused with 429: the provider is never contacted. Nothing is reserved
    when no provider is configured — the deterministic stubs cost nothing.

    A MISSING key already degrades to the deterministic stub before this is
    ever reached (``select_provider``); this only guards a key that IS
    present but the call still fails (expired, wrong scope, quota,
    unreachable) — failure modes deliberately not enumerated by type, since
    OpenAI/Anthropic/litellm each raise their own exception hierarchy and a
    provider's own message already says what's wrong.
    """
    if select_provider() is not None:
        block = bootstrap.get("llm") or {}
        budget = int(block.get("token_budget") or _DEFAULT_TOKEN_BUDGET)
        reserve = max(1, llm_calls) * max_tokens_per_call(bootstrap)
        if not _BUDGET.try_reserve(reserve, budget):
            if inspect.iscoroutine(coro):
                coro.close()
            await emit_event(
                "llm.refused", bootstrap, task=task, reason="token budget exhausted",
                budget=budget, spent=_BUDGET.spent, **extra,
            )
            raise HTTPException(status_code=429, detail="LLM token budget exhausted for this run")
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 — provider SDKs raise their own hierarchies
        detail = redact_secrets(str(exc))[:300]
        await emit_event("llm.failed", bootstrap, task=task, detail=detail, **extra)
        raise HTTPException(
            status_code=502, detail=f"LLM provider call failed: {detail}"
        ) from None
