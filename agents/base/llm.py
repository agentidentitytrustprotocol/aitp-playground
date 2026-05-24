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
"""
from __future__ import annotations

import os
from typing import Literal, Optional

Provider = Literal["openai", "anthropic"]

_OPENAI_DEFAULT = "gpt-4o-mini"
_ANTHROPIC_DEFAULT = "claude-sonnet-4-6"


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
        return ChatOpenAI(model=model_name("openai"), temperature=0.2)
    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        return ChatAnthropic(model=model_name("anthropic"))
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
    return LLM(model=f"{provider}/{model_name(provider)}")
