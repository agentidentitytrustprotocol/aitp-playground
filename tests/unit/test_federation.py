"""Fast unit coverage for the cross-domain (federated) building blocks that
don't need a live subprocess: the did:web http/https gate, the bootstrap
public-origin plumbing, and the fail-closed loopback guard helpers.

The full spawn-and-handshake path lives in
tests/integration/test_federated_handshake.py (gated on AITP_E2E).
"""
from __future__ import annotations

from aitp_playground.api.hosted import _is_loopback, _origin_of
from aitp_playground.config import Settings
from aitp_playground.hosting.bootstrap import BootstrapBuilder
from aitp_playground.registry.models import AgentSpec
from aitp_playground.registry.service import RegistryService
from aitp_playground.trust.resolver import _http_allowed


# ── did:web http/https gate ────────────────────────────────────────────────


def test_loopback_always_http(monkeypatch) -> None:
    monkeypatch.delenv("AITP_DIDWEB_INSECURE_HOSTS", raising=False)
    assert _http_allowed("localhost:8100") is True
    assert _http_allowed("127.0.0.1:8100") is True


def test_real_host_defaults_to_https(monkeypatch) -> None:
    monkeypatch.delenv("AITP_DIDWEB_INSECURE_HOSTS", raising=False)
    assert _http_allowed("org-b.aitp.test:9100") is False
    assert _http_allowed("org-b.aitp.test") is False


def test_insecure_hosts_suffix_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("AITP_DIDWEB_INSECURE_HOSTS", ".aitp.test")
    assert _http_allowed("org-b.aitp.test:9100") is True
    assert _http_allowed("org-a.aitp.test") is True
    # A host outside the suffix is still https-only.
    assert _http_allowed("evil.example.com") is False


def test_insecure_hosts_exact_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("AITP_DIDWEB_INSECURE_HOSTS", "org-b.aitp.test:9100")
    assert _http_allowed("org-b.aitp.test:9100") is True
    assert _http_allowed("org-c.aitp.test:9100") is False


# ── bootstrap public-origin plumbing ───────────────────────────────────────


def _manifest(settings: Settings):
    return RegistryService(settings).get_agent_manifest("_shared/agents/analyzer")


def test_bootstrap_defaults_to_localhost_origin() -> None:
    settings = Settings(cp_base_url="", cp_api_key="")
    builder = BootstrapBuilder(settings)
    bs = builder.build(
        run_id="r", agent_spec=AgentSpec(id="analyzer", ref="_shared/agents/analyzer"),
        resolved_manifest=_manifest(settings), port=8100, peers={}, inputs={},
    )
    assert bs["aitp"]["handshake_endpoint"] == "http://localhost:8100/aitp/handshake/hello"
    assert bs["aitp"]["did_web_scheme"] == "http"


def test_bootstrap_public_origin_advertises_remote_https() -> None:
    settings = Settings(cp_base_url="", cp_api_key="")
    builder = BootstrapBuilder(settings)
    bs = builder.build(
        run_id="r",
        agent_spec=AgentSpec(id="analyzer", ref="_shared/agents/analyzer", org="external"),
        resolved_manifest=_manifest(settings), port=8100, peers={}, inputs={},
        public_origin="https://org-b.aitp.test",
    )
    # The manifest the peer fetches must advertise the *public* origin, not
    # localhost, or the initiator dials its own loopback.
    assert bs["aitp"]["handshake_endpoint"] == "https://org-b.aitp.test/aitp/handshake/hello"
    assert bs["aitp"]["did_web_scheme"] == "https"


def test_bootstrap_public_origin_http_with_port() -> None:
    settings = Settings(cp_base_url="", cp_api_key="")
    builder = BootstrapBuilder(settings)
    bs = builder.build(
        run_id="r",
        agent_spec=AgentSpec(id="analyzer", ref="_shared/agents/analyzer", org="external"),
        resolved_manifest=_manifest(settings), port=8100, peers={}, inputs={},
        public_origin="http://org-b.aitp.test:9100",
    )
    assert bs["aitp"]["handshake_endpoint"] == "http://org-b.aitp.test:9100/aitp/handshake/hello"
    assert bs["aitp"]["did_web_scheme"] == "http"


# ── fail-closed guard helpers ──────────────────────────────────────────────


def test_origin_of_strips_well_known() -> None:
    assert _origin_of("https://org-b.aitp.test/.well-known/aitp-manifest") == "https://org-b.aitp.test"
    assert _origin_of("http://org-b.aitp.test:9100/.well-known/aitp-manifest") == "http://org-b.aitp.test:9100"


def test_is_loopback_detects_disguised_localhost() -> None:
    assert _is_loopback("http://localhost:9100/.well-known/aitp-manifest") is True
    assert _is_loopback("http://127.0.0.1:9100/.well-known/aitp-manifest") is True
    assert _is_loopback("https://org-b.aitp.test/.well-known/aitp-manifest") is False
