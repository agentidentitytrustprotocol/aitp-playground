"""Shared gating for tests that spend money on a real LLM provider.

A test marked ``@pytest.mark.live_llm`` (registered in ``pyproject.toml``) runs
only when all of these hold:

* ``CI`` is not set. CI never spends: no provider key exists there either
  (docker.yml copies ``.env.example``), so this is the second lock, for runs
  on a host that happens to export ``CI``.
* the provider selected by ``LLM_PROVIDER`` (default ``openai``) has its key
  set: ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY``. A key for the *other*
  provider does not count.
* ``AITP_LIVE_LLM=1``, which only ``docker-compose.live.yml`` sets (via
  ``scripts/live-integration.sh``). ``@pytest.mark.live_llm(live_flag=False)``
  drops this one requirement, for ``test_llm_e2e.py``, which keeps its older
  ``AITP_LLM_E2E=1`` gate.
"""
from __future__ import annotations

import os

import pytest

PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def selected_provider() -> str:
    return (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()


def live_llm_skip_reason(*, live_flag: bool = True) -> str | None:
    """Why a live-LLM test must not run here, or ``None`` when it may."""
    if os.environ.get("CI"):
        return "live LLM tests never run under CI (CI is set)"
    provider = selected_provider()
    key = PROVIDER_KEYS.get(provider)
    if key is None:
        return f"LLM_PROVIDER={provider!r} is not a known provider"
    if not os.environ.get(key):
        return f"{key} not set for LLM_PROVIDER={provider}; cannot exercise the real LLM path"
    if live_flag and os.environ.get("AITP_LIVE_LLM") != "1":
        return "live LLM test: run scripts/live-integration.sh (sets AITP_LIVE_LLM=1)"
    return None


def pytest_runtest_setup(item) -> None:
    mark = item.get_closest_marker("live_llm")
    if mark is None:
        return
    reason = live_llm_skip_reason(live_flag=mark.kwargs.get("live_flag", True))
    if reason:
        pytest.skip(reason)
