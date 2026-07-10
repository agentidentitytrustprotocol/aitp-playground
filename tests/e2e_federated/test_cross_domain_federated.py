"""Cross-domain federated e2e — drives two *separate* playground services on
distinct domains and proves a real cross-origin AITP handshake + capability
call between agents each hosted by a different service.

This is the true "two services, two domains" test. It's transport-agnostic:
the same assertions run against the Level 1 (http, ``.aitp.test`` hostnames)
and Level 2 (https via Caddy + local CA) stacks — only the compose file and
the agent-facing scheme differ. The control API (``/hosted-agents``) is reached
over the published host ports in both cases.

Prereqs — bring up a stack from ``federated/`` first, e.g. Level 1:

    docker compose -f federated/docker-compose.federated.yml up --build -d
    AITP_FEDERATED_E2E=1 uv run pytest tests/e2e_federated/ -v
    docker compose -f federated/docker-compose.federated.yml down

Level 2 is identical with ``docker-compose.federated-tls.yml``.

Env knobs (all optional; defaults match the compose files):
    ORG_A_URL   control API of org-A          (default http://localhost:18000)
    ORG_B_URL   control API of org-B          (default http://localhost:18001)
    FEDERATED_AGENT_PORT  pinned agent listen port (default 9100)
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AITP_FEDERATED_E2E"),
    reason="Two-service federated stack — set AITP_FEDERATED_E2E=1 (and bring the stack up)",
)

ORG_A_URL = os.environ.get("ORG_A_URL", "http://localhost:18000")
ORG_B_URL = os.environ.get("ORG_B_URL", "http://localhost:18001")
AGENT_PORT = int(os.environ.get("FEDERATED_AGENT_PORT", "9100"))


def _wait_healthy(url: str, timeout: float = 60.0) -> None:
    import time

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/healthz", timeout=5.0)
            if r.status_code == 200:
                return
            last = f"{r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(1.0)
    pytest.fail(f"service {url} not healthy within {timeout}s (last: {last})")


def _host(url: str, ref: str) -> dict:
    r = httpx.post(
        f"{url}/hosted-agents",
        json={"ref": ref, "port": AGENT_PORT},
        timeout=60.0,
    )
    assert r.status_code == 200, f"host {ref} on {url}: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture(scope="module")
def stack() -> dict:
    _wait_healthy(ORG_A_URL)
    _wait_healthy(ORG_B_URL)
    analyzer = _host(ORG_B_URL, "_shared/agents/analyzer")
    researcher = _host(ORG_A_URL, "_shared/agents/researcher")
    try:
        yield {"analyzer": analyzer, "researcher": researcher}
    finally:
        httpx.delete(f"{ORG_B_URL}/hosted-agents/{analyzer['hosted_id']}", timeout=30.0)
        httpx.delete(f"{ORG_A_URL}/hosted-agents/{researcher['hosted_id']}", timeout=30.0)


def test_analyzer_advertises_remote_origin(stack: dict) -> None:
    analyzer = stack["analyzer"]
    # The analyzer must present a did:web identity at org-B's domain — never
    # localhost — or org-A could never resolve it across the boundary.
    assert analyzer["did"].startswith("did:web:org-b.aitp.test")
    assert "localhost" not in analyzer["origin"]
    assert "127.0.0.1" not in analyzer["origin"]


def test_cross_domain_handshake(stack: dict) -> None:
    researcher, analyzer = stack["researcher"], stack["analyzer"]
    r = httpx.post(
        f"{ORG_A_URL}/hosted-agents/{researcher['hosted_id']}/resolve-and-handshake",
        json={"peer_did": analyzer["did"], "requested_grants": ["analyze.data"]},
        timeout=60.0,
    )
    assert r.status_code == 200, f"handshake: {r.status_code} {r.text}"
    body = r.json()
    assert body["trust"] == "established"
    # Resolution crossed to org-B's real origin (fail-closed guard passed with
    # NO loopback opt-out — this is the guarantee the in-process test simulates).
    assert body["peer_origin"].endswith("org-b.aitp.test") or "org-b.aitp.test" in body["peer_origin"]
    assert "localhost" not in body["peer_origin"] and "127.0.0.1" not in body["peer_origin"]
    assert body["peer_aid"] == analyzer["aid"]
    assert "analyze.data" in body["grants"]


def test_cross_domain_capability_call(stack: dict) -> None:
    researcher, analyzer = stack["researcher"], stack["analyzer"]
    # Ensure trust is established (idempotent — re-handshake is cheap).
    hs = httpx.post(
        f"{ORG_A_URL}/hosted-agents/{researcher['hosted_id']}/resolve-and-handshake",
        json={"peer_did": analyzer["did"], "requested_grants": ["analyze.data"]},
        timeout=60.0,
    ).json()

    r = httpx.post(
        f"{ORG_A_URL}/hosted-agents/{researcher['hosted_id']}/invoke",
        json={
            "peer_port": hs["peer_port"],
            "peer_base_url": hs["peer_base_url"],
            "capability": "analyze.data",
            "payload": {"text": "AITP enables cross-org agent trust."},
        },
        timeout=120.0,
    )
    assert r.status_code == 200, f"invoke: {r.status_code} {r.text}"
    result = r.json()["result"]
    assert isinstance(result, dict) and not result.get("error"), result
    assert result.get("agent") == "analyzer"


def test_fail_closed_on_loopback_peer(stack: dict) -> None:
    """A did:web that would resolve to org-A's own loopback must be refused —
    the whole point of the exercise is that trust crossed a real boundary."""
    researcher = stack["researcher"]
    r = httpx.post(
        f"{ORG_A_URL}/hosted-agents/{researcher['hosted_id']}/resolve-and-handshake",
        json={"peer_did": f"did:web:localhost%3A{AGENT_PORT}"},
        timeout=60.0,
    )
    # Either the loopback guard (409) or a resolution/origin-mismatch error —
    # never a 200 "trust established".
    assert r.status_code != 200, f"loopback handshake should be refused, got 200: {r.text}"
