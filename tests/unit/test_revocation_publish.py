"""The issuer side of revocation: each agent signs and serves its own list.

What is pinned here, in the order a reader is likely to doubt it:

* the served list verifies against **this agent's own AID** (RFC-AITP-0008
  §1.5, rev-011: a list is valid only from the issuer of the tokens it names);
* it carries exactly what the CP relayed, and a later pull replaces it;
* with the CP down the last list is served unchanged until its `expires_at`,
  and after that, or when nothing was ever pulled, the route answers 503 —
  never an empty list;
* with no CP at all an empty signed list is served, so every agent has one.

Verification uses the SDK's `verify_revocation_list`, the same call a peer
verifier makes. The CP is an in-process `MockTransport`.
"""
from __future__ import annotations

import json
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import revocation_publish  # noqa: E402
from aitp_server import AitpServer  # noqa: E402
from revocation_publish import REVOCATION_LIST_PATH  # noqa: E402

JTI_A = str(uuid.uuid4())
JTI_B = str(uuid.uuid4())


def _bootstrap(*, cp: bool = True, ttl: int = 3600) -> dict[str, Any]:
    bootstrap: dict[str, Any] = {
        "run_id": "r",
        "agent_id": "issuer",
        "aitp": {
            "seed_hex": "66" * 32,
            "display_name": "issuer",
            "handshake_endpoint": "https://localhost:9/aitp/handshake/hello",
            "offered_caps": ["demo.write"],
        },
    }
    if cp:
        bootstrap["cp"] = {"base_url": "http://cp", "api_key": "admin-key", "list_ttl_secs": ttl}
    return bootstrap


def _server(**bootstrap_kwargs: Any) -> AitpServer:
    agent = aitp.AitpAgent.generate()
    manifest = agent.build_manifest(
        display_name="issuer",
        handshake_endpoint="https://localhost:9/aitp/handshake/hello",
        offered_caps=["demo.write"],
    )
    return AitpServer(
        agent=agent, manifest_json=manifest, port=9, bootstrap=_bootstrap(**bootstrap_kwargs)
    )


def _client(server: AitpServer) -> TestClient:
    app = FastAPI()
    app.include_router(server.router)
    return TestClient(app)


class FakeCp:
    """`GET /api/revocations` as CP1.1 serves it, with a switchable failure."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.fail: Callable[[], httpx.Response] | None = None
        self.requests: list[httpx.Request] = []
        self.answer_for: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            return self.fail()
        assert request.url.path == "/api/revocations"
        iss = self.answer_for or request.url.params["iss"]
        return httpx.Response(200, json={"iss": iss, "entries": self.entries})


@pytest.fixture
def cp(monkeypatch: pytest.MonkeyPatch) -> FakeCp:
    fake = FakeCp()

    def async_client_factory(**_kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(
        revocation_publish, "httpx",
        types.SimpleNamespace(AsyncClient=async_client_factory, HTTPError=httpx.HTTPError),
    )
    return fake


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def emit(event_type: str, _bootstrap: dict[str, Any], **fields: Any) -> None:
        seen.append({"type": event_type, **fields})

    import aitp_server
    monkeypatch.setattr(aitp_server, "emit_event", emit)
    return seen


def _served(server: AitpServer) -> httpx.Response:
    return _client(server).get(REVOCATION_LIST_PATH)


def _jtis(envelope_json: str) -> list[str]:
    return [e["jti"] for e in json.loads(envelope_json)["revocation_list"]["entries"]]


# ── what is served ──────────────────────────────────────────────────────


async def test_the_list_is_signed_by_the_agent_itself_and_carries_the_cp_entries(
    cp: FakeCp, events: list[dict[str, Any]],
) -> None:
    cp.entries = [{"jti": JTI_A, "revoked_at": 1_790_000_000, "reason": "compromised"}]
    server = _server()

    result = await server.publish_revocations_now(quiet=False)

    assert result == {"published": True, "entry_count": 1}
    assert cp.requests[0].url.params["iss"] == server.agent.aid
    assert cp.requests[0].headers["authorization"] == "Bearer admin-key"
    resp = _served(server)
    assert resp.status_code == 200
    aitp.verify_revocation_list(resp.text, server.agent.aid)
    body = json.loads(resp.text)["revocation_list"]
    assert body["issuer"] == server.agent.aid
    assert body["entries"] == [
        {"jti": JTI_A, "revoked_at": 1_790_000_000, "reason": "compromised"}
    ]
    assert [e["type"] for e in events] == ["revocation.list_published"]


async def test_a_later_pull_replaces_the_list_rather_than_merging(cp: FakeCp, events) -> None:
    server = _server()
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await server.publish_revocations_now(quiet=True)
    cp.entries = [{"jti": JTI_B, "revoked_at": 2}]
    await server.publish_revocations_now(quiet=True)

    assert _jtis(_served(server).text) == [JTI_B]


async def test_an_empty_cp_answer_is_served_as_an_empty_signed_list(cp: FakeCp, events) -> None:
    server = _server()
    await server.publish_revocations_now(quiet=True)

    resp = _served(server)
    assert resp.status_code == 200
    aitp.verify_revocation_list(resp.text, server.agent.aid)
    assert _jtis(resp.text) == []


def test_without_a_cp_every_agent_still_serves_an_empty_signed_list() -> None:
    server = _server(cp=False)

    resp = _served(server)

    assert resp.status_code == 200
    aitp.verify_revocation_list(resp.text, server.agent.aid)
    assert _jtis(resp.text) == []


# ── the CP is unavailable ───────────────────────────────────────────────


def test_nothing_pulled_yet_is_503_not_an_empty_list(cp: FakeCp) -> None:
    server = _server()

    resp = _served(server)

    assert resp.status_code == 503
    assert resp.json()["code"] == "REVOCATION_UNAVAILABLE"
    assert resp.headers["retry-after"] == "30"


@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.Response(
            503, json={"code": "REVOCATION_UNAVAILABLE"}, headers={"Retry-After": "30"}
        ),
        lambda: httpx.Response(403, json={"code": "ISSUER_MISMATCH"}),
        lambda: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    ],
    ids=["503", "403", "unreachable"],
)
async def test_a_failed_pull_keeps_serving_the_last_list_byte_for_byte(
    cp: FakeCp, events: list[dict[str, Any]], failure,
) -> None:
    server = _server()
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await server.publish_revocations_now(quiet=True)
    before = _served(server).text

    cp.fail = failure
    result = await server.publish_revocations_now(quiet=False)

    assert result["published"] is False
    assert _served(server).text == before
    assert events[-1]["type"] == "revocation.publish_failed"
    assert events[-1]["serving"] is True


async def test_the_cp_error_code_is_reported(cp: FakeCp, events) -> None:
    cp.fail = lambda: httpx.Response(503, json={"code": "REVOCATION_UNAVAILABLE"})
    result = await _server().publish_revocations_now(quiet=True)
    assert result["cause"] == "REVOCATION_UNAVAILABLE"


async def test_past_expires_at_the_list_stops_being_served(cp: FakeCp, events) -> None:
    server = _server(ttl=60)
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await server.publish_revocations_now(quiet=True)
    cp.fail = lambda: httpx.Response(503, json={"code": "REVOCATION_UNAVAILABLE"})
    await server.publish_revocations_now(quiet=True)

    assert server.issued_revocations.serveable() is not None
    assert server.issued_revocations.serveable(now=time.time() + 61) is None


# ── answers that must not become a list ─────────────────────────────────


async def test_an_answer_for_another_issuer_is_refused(cp: FakeCp, events) -> None:
    cp.answer_for = aitp.AitpAgent.generate().aid
    server = _server()

    result = await server.publish_revocations_now(quiet=True)

    assert result["cause"] == "issuer_mismatch"
    assert _served(server).status_code == 503


async def test_a_non_uuid_jti_refuses_the_whole_pull_rather_than_dropping_it(
    cp: FakeCp, events,
) -> None:
    server = _server()
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await server.publish_revocations_now(quiet=True)
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}, {"jti": "not-a-uuid", "revoked_at": 2}]

    result = await server.publish_revocations_now(quiet=True)

    assert result["cause"] == "malformed_body"
    assert _jtis(_served(server).text) == [JTI_A]


# ── identity ────────────────────────────────────────────────────────────


async def test_a_restart_from_the_same_seed_signs_as_the_same_issuer(cp: FakeCp, events) -> None:
    seed = bytes.fromhex("77" * 32)
    lists = []
    for _ in range(2):
        agent = aitp.AitpAgent.from_seed(seed)
        server = AitpServer(
            agent=agent, manifest_json="{}", port=9, bootstrap=_bootstrap(),
        )
        await server.publish_revocations_now(quiet=True)
        lists.append(_served(server).text)

    first_issuer = json.loads(lists[0])["revocation_list"]["issuer"]
    aitp.verify_revocation_list(lists[1], first_issuer)


async def test_a_key_rotation_re_signs_the_list_for_the_new_aid(cp: FakeCp, events) -> None:
    server = _server()
    await server.publish_revocations_now(quiet=True)
    client = _client(server)

    rotated = client.post("/admin/rotate-keys").json()

    resp = client.get(REVOCATION_LIST_PATH)
    assert resp.status_code == 200
    aitp.verify_revocation_list(resp.text, rotated["aid"])
    assert cp.requests[-1].url.params["iss"] == rotated["aid"]
