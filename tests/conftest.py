"""pytest setup: ensure src and agents/base are importable."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
AGENT_BASE = ROOT / "agents" / "base"
for p in (SRC, AGENT_BASE):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

# Make sure tests use the scenarios shipped in this repo, not a cwd-dependent path.
os.environ.setdefault("SCENARIOS_DIR", str(ROOT / "scenarios"))

# Authenticated routes need a configured key (unset fails closed with 503).
# The limits are opened wide so ordinary tests never trip them; the limit
# tests build their own Settings with tight values.
from tests._auth import TEST_API_KEY  # noqa: E402

os.environ.setdefault("PLAYGROUND_API_KEY", TEST_API_KEY)
os.environ.setdefault("RATE_LIMIT_BURST", "100000")
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("MAX_RUNS_PER_HOUR", "100000")


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "sdk_feature(key): the test drives a real optional aitp-sdk feature",
    )
    config.addinivalue_line(
        "markers",
        "sdk_feature_expected_missing(key): AITP_EXPECT_EXPERIMENTAL=1 but the SDK lacks key",
    )


def pytest_runtest_setup(item) -> None:
    """Fail a test marked by tests._sdk_features in expect mode."""
    import pytest

    mark = item.get_closest_marker("sdk_feature_expected_missing")
    if mark is not None:
        pytest.fail(
            f"aitp-sdk lacks {mark.args[0]} but AITP_EXPECT_EXPERIMENTAL=1",
            pytrace=False,
        )
