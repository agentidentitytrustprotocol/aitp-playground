"""Fast unit coverage for the cross-domain (federated) building blocks that
don't need a live subprocess: the did:web http/https gate, the bootstrap
public-origin plumbing, and the fail-closed loopback guard helpers.

The full spawn-and-handshake path lives in
tests/integration/test_federated_handshake.py (gated on AITP_E2E).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aitp_playground.api._deps import get_hosted_manager
from aitp_playground.api.hosted import _is_loopback, _origin_of, router as hosted_router
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


# ── `/hosted-agents/{id}/resolve-and-handshake` — route-level, not just the
# helpers in isolation (Phase 7 of plans/audit-2026-08-28-cleanup.md) ──────


class _FakeHostedAgent:
    def __init__(self, port: int = 8100) -> None:
        self.port = port


class _FakeHostedManager:
    def get(self, hosted_id: str):
        return _FakeHostedAgent()


def _hosted_app() -> FastAPI:
    app = FastAPI()
    app.include_router(hosted_router)
    app.dependency_overrides[get_hosted_manager] = lambda: _FakeHostedManager()
    return app


def test_resolve_and_handshake_rejects_a_non_did_web_peer(monkeypatch) -> None:
    """Item 8: `body.peer_did.startswith("did:web:")` — a bare AID or any
    other identifier shape must not reach did:web resolution at all."""
    client = TestClient(_hosted_app())
    resp = client.post(
        "/hosted-agents/h1/resolve-and-handshake",
        json={"peer_did": "aid:pubkey:not-a-did-web"},
    )
    assert resp.status_code == 400, resp.text
    assert "did:web" in resp.text.lower()


def test_resolve_and_handshake_refuses_a_disguised_loopback_peer(monkeypatch) -> None:
    """Item 9: the fail-closed loopback guard, exercised through the ROUTE —
    not just `_is_loopback` in isolation (`test_is_loopback_detects_disguised_localhost`
    above). Before this test, deleting `and not allow_loopback` from the
    route's guard passed all of `tests/unit`."""
    monkeypatch.delenv("AITP_FEDERATION_ALLOW_LOOPBACK", raising=False)

    async def _fake_resolve(peer_did: str) -> str:
        return "http://localhost:9999/.well-known/aitp-manifest"

    monkeypatch.setattr("aitp_playground.api.hosted.resolve_did_web", _fake_resolve)

    client = TestClient(_hosted_app())
    resp = client.post(
        "/hosted-agents/h1/resolve-and-handshake",
        json={"peer_did": "did:web:localhost%3A9999"},
    )
    assert resp.status_code == 409, resp.text
    assert "loopback" in resp.text.lower()


def test_resolve_and_handshake_allows_loopback_under_the_explicit_opt_out(monkeypatch) -> None:
    """The other direction of item 9 — the opt-out itself must still work,
    or fixing the guard's enforcement could accidentally break the in-process
    federated integration suite that depends on it."""
    monkeypatch.setenv("AITP_FEDERATION_ALLOW_LOOPBACK", "1")

    async def _fake_resolve(peer_did: str) -> str:
        return "http://localhost:9999/.well-known/aitp-manifest"

    monkeypatch.setattr("aitp_playground.api.hosted.resolve_did_web", _fake_resolve)

    client = TestClient(_hosted_app())
    resp = client.post(
        "/hosted-agents/h1/resolve-and-handshake",
        json={"peer_did": "did:web:localhost%3A9999"},
    )
    # Past the loopback guard now — next is the origin-mismatch check, which
    # this DID (localhost:9999, matching the resolved origin) also clears,
    # so it proceeds to actually dial the hosted agent's admin route and
    # fails there instead (no such route running in this test) — anything
    # other than 409 proves the loopback guard itself let it through.
    assert resp.status_code != 409, resp.text


def test_resolve_and_handshake_rejects_a_did_web_origin_mismatch(monkeypatch) -> None:
    """Item 7: the resolved manifest's origin must match the DID's own host
    component — a DID resolving somewhere it does not claim to."""
    monkeypatch.setenv("AITP_FEDERATION_ALLOW_LOOPBACK", "1")  # isolate this check alone

    async def _fake_resolve(peer_did: str) -> str:
        return "http://org-b.aitp.test:9100/.well-known/aitp-manifest"

    monkeypatch.setattr("aitp_playground.api.hosted.resolve_did_web", _fake_resolve)

    client = TestClient(_hosted_app())
    resp = client.post(
        "/hosted-agents/h1/resolve-and-handshake",
        json={"peer_did": "did:web:org-c.aitp.test%3A9100"},
    )
    assert resp.status_code == 409, resp.text
    assert "origin mismatch" in resp.text.lower()
