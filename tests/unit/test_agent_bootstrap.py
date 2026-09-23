"""Unit tests for the agent-side bootstrap helpers (agents/base/bootstrap.py).

Every agent worker calls `create_agent` / `get_manifest_json` at import time
to derive its keypair and self-signed manifest from the bootstrap file the
supervisor writes for it — before this, both had zero fast-suite coverage;
they were only exercised by actually spawning an agent subprocess in the
gated e2e suite.

NOT the same module as `test_bootstrap.py`, which tests
`aitp_playground.hosting.bootstrap.BootstrapBuilder` — the *supervisor* side
in `src/`, which writes the file this module reads. This file is the *agent*
side, in `agents/base/`, which reads it back and derives identity from it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

from bootstrap import create_agent, get_manifest_json, load_bootstrap  # noqa: E402


def _bootstrap(**aitp_overrides) -> dict:
    cfg = {
        "seed_hex": "42" * 32,
        "display_name": "researcher",
        "handshake_endpoint": "http://localhost:8100/aitp/handshake/hello",
        "offered_caps": ["research.query"],
    }
    cfg.update(aitp_overrides)
    return {"run_id": "r", "agent_id": "researcher", "aitp": cfg}


# ── create_agent ─────────────────────────────────────────────────────────


def test_create_agent_derives_a_deterministic_identity_from_the_seed() -> None:
    """The realistic "duplicate identity" case for this function: the same
    seed_hex — e.g. a supervisor restarting the same agent — must always
    rederive the SAME AID, not a fresh one. There is no dedup registry here;
    determinism from the seed *is* the identity-stability guarantee."""
    bs = _bootstrap()
    first = create_agent(bs)
    second = create_agent(bs)
    assert first.aid == second.aid
    assert first.aid.startswith("aid:pubkey:")


def test_create_agent_with_a_different_seed_derives_a_different_identity() -> None:
    a = create_agent(_bootstrap(seed_hex="11" * 32))
    b = create_agent(_bootstrap(seed_hex="22" * 32))
    assert a.aid != b.aid


def test_create_agent_defaults_to_ed25519_when_signing_suite_is_absent() -> None:
    agent = create_agent(_bootstrap())
    assert ":p256:" not in agent.aid


def test_create_agent_honours_an_explicit_signing_suite() -> None:
    agent = create_agent(_bootstrap(signing_suite="p256"))
    assert agent.aid.startswith("aid:pubkey:p256:")


# ── get_manifest_json ─────────────────────────────────────────────────────


def test_get_manifest_json_builds_a_self_signed_manifest_that_verifies() -> None:
    bs = _bootstrap()
    agent = create_agent(bs)
    manifest_json = get_manifest_json(agent, bs)

    aitp.verify_manifest_json(manifest_json)  # must not raise
    body = json.loads(manifest_json)["manifest"]
    assert body["aid"] == agent.aid
    assert body["display_name"] == "researcher"
    assert body["handshake_endpoint"] == "http://localhost:8100/aitp/handshake/hello"
    assert body["offered_capabilities"] == ["research.query"]
    assert body["identity_hint"]["type"] == "pinned_key"


def test_get_manifest_json_defaults_ttl_to_one_hour_when_unset() -> None:
    bs = _bootstrap()
    agent = create_agent(bs)
    body = json.loads(get_manifest_json(agent, bs))["manifest"]
    assert int(body["expires_at"]) - int(body["published_at"]) == 3600


def test_get_manifest_json_honours_an_explicit_ttl() -> None:
    bs = _bootstrap(ttl_secs=60)
    agent = create_agent(bs)
    body = json.loads(get_manifest_json(agent, bs))["manifest"]
    assert int(body["expires_at"]) - int(body["published_at"]) == 60


def test_get_manifest_json_builds_an_oidc_manifest_when_identity_type_is_oidc() -> None:
    """The branch that sets identity_type/oidc_issuer/oidc_subject — an agent
    hosted with OIDC identity must actually get an OIDC identity_hint, not
    the pinned_key default."""
    bs = _bootstrap(
        identity_type="oidc",
        oidc_issuer="https://idp.aitp-playground.local/",
        oidc_subject="alice",
    )
    agent = create_agent(bs)
    body = json.loads(get_manifest_json(agent, bs))["manifest"]
    assert body["identity_hint"] == {
        "type": "oidc",
        "subject": "alice",
        "issuer": "https://idp.aitp-playground.local/",
    }


# ── load_bootstrap ────────────────────────────────────────────────────────


def test_load_bootstrap_raises_a_clear_error_without_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AITP_BOOTSTRAP_FILE", raising=False)
    with pytest.raises(RuntimeError, match="AITP_BOOTSTRAP_FILE"):
        load_bootstrap()


def test_load_bootstrap_reads_and_parses_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _bootstrap()
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("AITP_BOOTSTRAP_FILE", str(path))
    assert load_bootstrap() == payload
