"""Deterministic per-run agent seed derivation."""
from __future__ import annotations

import hashlib


def derive_seed_hex(run_id: str, agent_id: str, *, org: str = "internal") -> str:
    """Produce a 32-byte (64 hex char) seed deterministic for a (run_id, agent_id, org) triple.

    Cross-org scenarios derive 'external' agents under a separate namespace so the
    resulting AIDs look like they come from a different org.
    """
    material = f"{org}:{run_id}:{agent_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()
