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
