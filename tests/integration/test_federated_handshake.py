"""Cross-domain federated handshake — the in-process fidelity of the Level 1/2
Docker stack, runnable without Docker.

Two agents are hosted at *distinct origins* (two real sockets on 127.0.0.1),
the researcher resolves the analyzer via did:web, and a real AITP handshake +
capability call crosses the boundary. The point the plain scenario suite can't
make: resolution is exercised for real and the call dials the *peer's* origin,
not our own loopback shortcut.

Because both origins are on 127.0.0.1 here, the fail-closed loopback guard is
explicitly opted out via AITP_FEDERATION_ALLOW_LOOPBACK — and the test also
asserts that with the opt-out removed the handshake is refused, which is the
guarantee the Docker stack relies on with real hostnames.

Gated on AITP_E2E=1 (spawns subprocesses; ~30-45s). Run with:

    AITP_E2E=1 uv run pytest tests/integration/test_federated_handshake.py -v
"""
from __future__ import annotations

import os
import socket

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AITP_E2E"),
    reason="Live subprocess test — set AITP_E2E=1 to enable",
)

pytest.importorskip("aitp")

from fastapi.testclient import TestClient  # noqa: E402

from aitp_playground.main import create_app  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _host(client: TestClient, ref: str, port: int) -> dict:
    r = client.post(
        "/hosted-agents",
        json={
            "ref": ref,
            "public_host": f"127.0.0.1:{port}",
            "public_scheme": "http",
            "port": port,
        },
    )
    assert r.status_code == 200, f"host {ref} failed: {r.status_code} {r.text}"
    return r.json()


def test_cross_domain_handshake_and_capability(monkeypatch) -> None:
    monkeypatch.setenv("AITP_FEDERATION_ALLOW_LOOPBACK", "1")

    port_a = _free_port()  # researcher (org-A / initiator)
    port_b = _free_port()  # analyzer  (org-B / responder)

    with TestClient(create_app()) as client:
        analyzer = _host(client, "_shared/agents/analyzer", port_b)
        researcher = _host(client, "_shared/agents/researcher", port_a)

        # The analyzer advertises its public origin, not localhost.
        assert analyzer["origin"] == f"http://127.0.0.1:{port_b}"
        assert analyzer["did"] == f"did:web:127.0.0.1%3A{port_b}"
        assert analyzer["handshake_url"] == f"http://127.0.0.1:{port_b}/aitp/handshake/hello"

        # ── cross-domain handshake ──────────────────────────────────────────
        hs = client.post(
            f"/hosted-agents/{researcher['hosted_id']}/resolve-and-handshake",
            json={"peer_did": analyzer["did"], "requested_grants": ["analyze.data"]},
        )
        assert hs.status_code == 200, f"handshake failed: {hs.status_code} {hs.text}"
        hb = hs.json()
        assert hb["trust"] == "established"
        # Resolution actually crossed to the analyzer's origin.
        assert hb["peer_origin"] == f"http://127.0.0.1:{port_b}"
        assert hb["resolved_manifest_url"] == f"http://127.0.0.1:{port_b}/.well-known/aitp-manifest"
        # The TCT was minted by the analyzer's real AID with the requested grant.
        assert hb["peer_aid"] == analyzer["aid"]
        assert "analyze.data" in hb["grants"]

        # ── cross-domain capability call ────────────────────────────────────
        inv = client.post(
            f"/hosted-agents/{researcher['hosted_id']}/invoke",
            json={
                "peer_port": hb["peer_port"],
                "peer_base_url": hb["peer_base_url"],
                "capability": "analyze.data",
                "payload": {"text": "AITP enables cross-org agent trust."},
            },
        )
        assert inv.status_code == 200, f"invoke failed: {inv.status_code} {inv.text}"
        result = inv.json()["result"]
        assert isinstance(result, dict) and not result.get("error"), result
        # The response came from the analyzer subprocess on org-B.
        assert result.get("agent") == "analyzer"

        # ── fail-closed guarantee ───────────────────────────────────────────
        # Remove the loopback opt-out: the same handshake must now be refused,
        # because in production a did:web that resolves to loopback is a
        # same-process handshake in disguise.
        monkeypatch.delenv("AITP_FEDERATION_ALLOW_LOOPBACK", raising=False)
        refused = client.post(
            f"/hosted-agents/{researcher['hosted_id']}/resolve-and-handshake",
            json={"peer_did": analyzer["did"], "requested_grants": ["analyze.data"]},
        )
        assert refused.status_code == 409, (
            f"expected fail-closed 409, got {refused.status_code}: {refused.text}"
        )
        assert "loopback" in refused.text.lower()
