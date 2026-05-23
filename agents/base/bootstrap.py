"""Read AITP_BOOTSTRAP_FILE and construct an AitpAgent via aitp-py."""
from __future__ import annotations

import json
import os
from typing import Any

import aitp


def load_bootstrap() -> dict[str, Any]:
    path = os.environ.get("AITP_BOOTSTRAP_FILE", "")
    if not path:
        raise RuntimeError("AITP_BOOTSTRAP_FILE not set")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def create_agent(bootstrap: dict[str, Any]) -> "aitp.AitpAgent":
    seed_hex: str = bootstrap["aitp"]["seed_hex"]
    return aitp.AitpAgent.from_seed(bytes.fromhex(seed_hex))


def get_manifest_json(agent: "aitp.AitpAgent", bootstrap: dict[str, Any]) -> str:
    cfg = bootstrap["aitp"]
    return agent.build_manifest(
        display_name=cfg["display_name"],
        handshake_endpoint=cfg["handshake_endpoint"],
        offered_caps=list(cfg["offered_caps"]),
        ttl_secs=int(cfg.get("ttl_secs", 3600)),
    )
