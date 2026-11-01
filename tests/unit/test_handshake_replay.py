"""RFC-AITP-0001 §5.5 on the handshake routes, in the RFC-AITP-0009 §3.1 order.

The guard runs before the SDK sees an envelope: a seen `message_id` is
`REPLAY_DETECTED`, then a timestamp outside the window is `TIMESTAMP_EXPIRED`.
The route tests drive a real `AitpServer` with envelopes a real initiator built.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import aitp_server  # noqa: E402
import bootstrap as agent_bootstrap  # noqa: E402
from aitp_server import AitpServer  # noqa: E402
from handshake_replay import HandshakeRejected, ReplayGuard  # noqa: E402
from revocation_state import RevocationState  # noqa: E402

GRANT = "demo.write"


def _envelope(message_id: str | None = None, timestamp: Any = None) -> str:
    return json.dumps({
        "message_id": message_id or str(uuid.uuid4()),
        "timestamp": int(time.time()) if timestamp is None else timestamp,
    })


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


# ── the guard ───────────────────────────────────────────────────────────


def test_a_message_id_is_admitted_once() -> None:
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    env = _envelope(timestamp=clock.now)

    guard.check(env)
    with pytest.raises(HandshakeRejected) as exc:
        guard.check(env)
    assert exc.value.code == "REPLAY_DETECTED"


def test_dedup_keys_on_message_id_not_the_bytes() -> None:
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    mid = str(uuid.uuid4())
    guard.check(json.dumps({"message_id": mid, "timestamp": clock.now}))

    reserialised = json.dumps({"timestamp": clock.now, "message_id": mid}, indent=2)
    with pytest.raises(HandshakeRejected) as exc:
        guard.check(reserialised)
    assert exc.value.code == "REPLAY_DETECTED"


@pytest.mark.parametrize("skew", [301, -301])
def test_a_timestamp_outside_the_window_is_expired(skew: int) -> None:
    clock = Clock()
    with pytest.raises(HandshakeRejected) as exc:
        ReplayGuard(clock=clock).check(_envelope(timestamp=clock.now + skew))
    assert exc.value.code == "TIMESTAMP_EXPIRED"


@pytest.mark.parametrize("timestamp", ["1800000000", True, None])
def test_a_timestamp_that_is_not_a_number_is_expired(timestamp: Any) -> None:
    clock = Clock()
    env = json.dumps({"message_id": str(uuid.uuid4()), "timestamp": timestamp})
    with pytest.raises(HandshakeRejected) as exc:
        ReplayGuard(clock=clock).check(env)
    assert exc.value.code == "TIMESTAMP_EXPIRED"


def test_replay_is_checked_before_the_timestamp() -> None:
    """§3.1 step 1 before step 3: a stale replay reports the replay."""
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    env = _envelope(timestamp=clock.now)
    guard.check(env)
    clock.now += 299

    with pytest.raises(HandshakeRejected) as exc:
        guard.check(env)
    assert exc.value.code == "REPLAY_DETECTED"


def test_an_id_is_forgotten_only_once_its_timestamp_has_left_the_window() -> None:
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    env = _envelope(timestamp=clock.now)
    guard.check(env)

    clock.now += 301
    with pytest.raises(HandshakeRejected) as exc:
        guard.check(env)
    assert exc.value.code == "TIMESTAMP_EXPIRED"
    assert guard._seen == {}


def test_a_future_dated_id_is_held_until_its_window_closes() -> None:
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    env = _envelope(timestamp=clock.now + 300)
    guard.check(env)

    clock.now += 599
    with pytest.raises(HandshakeRejected) as exc:
        guard.check(env)
    assert exc.value.code == "REPLAY_DETECTED"


def test_a_stale_envelope_is_not_recorded() -> None:
    clock = Clock()
    guard = ReplayGuard(clock=clock)
    with pytest.raises(HandshakeRejected):
        guard.check(_envelope(timestamp=clock.now - 301))
    assert guard._seen == {}


@pytest.mark.parametrize("body", ["not json", "[]", json.dumps({"timestamp": 1})])
def test_an_envelope_without_a_readable_message_id_is_invalid(body: str) -> None:
    with pytest.raises(HandshakeRejected) as exc:
        ReplayGuard().check(body)
    assert exc.value.code == "INVALID_ENVELOPE"


def test_a_full_deny_list_refuses_instead_of_forgetting() -> None:
    clock = Clock()
    guard = ReplayGuard(clock=clock, max_entries=2)
    guard.check(_envelope(timestamp=clock.now))
    guard.check(_envelope(timestamp=clock.now))

    with pytest.raises(HandshakeRejected) as exc:
        guard.check(_envelope(timestamp=clock.now))
    assert (exc.value.code, exc.value.status) == (None, 429)


# ── the routes ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(aitp_server, "emit_event", emit)


def _responder() -> tuple[AitpServer, httpx.AsyncClient]:
    bootstrap = {
        "run_id": "r", "agent_id": "b",
        "aitp": {
            "seed_hex": uuid.uuid4().hex * 2, "display_name": "b",
            "handshake_endpoint": "https://b/aitp/handshake/hello",
            "offered_caps": [GRANT],
        },
    }
    agent = agent_bootstrap.create_agent(bootstrap)
    server = AitpServer(
        agent=agent, manifest_json=agent_bootstrap.get_manifest_json(agent, bootstrap),
        port=9, bootstrap=bootstrap, revocation=RevocationState(),
    )
    app = FastAPI()
    app.include_router(server.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b")
    return server, client


def _initiator(server: AitpServer) -> "aitp.InitiatorSession":
    a = aitp.AitpAgent.generate()
    a.build_manifest(display_name="a", handshake_endpoint="https://a/x", offered_caps=["demo.read"])
    server.trusted_peer_aids.add(a.aid)
    return a.new_session(pinned_keys=[server.agent.aid])


async def test_a_replayed_hello_is_refused_before_a_second_session_opens() -> None:
    server, b = _responder()
    hello = _initiator(server).build_hello(server.manifest_json, [GRANT])

    first = await b.post("/aitp/handshake/hello", content=hello)
    assert first.status_code == 200, first.text
    replay = await b.post("/aitp/handshake/hello", content=hello)

    assert replay.status_code == 400
    assert replay.json()["code"] == "REPLAY_DETECTED"
    assert len(server._sessions) == 1


async def test_a_replayed_commit_is_refused() -> None:
    server, b = _responder()
    session = _initiator(server)
    hello = session.build_hello(server.manifest_json, [GRANT])
    ack = await b.post("/aitp/handshake/hello", content=hello)
    sid = ack.headers["x-aitp-session-id"]
    commit = session.process_hello_ack(ack.text, sid)

    done = await b.post("/aitp/handshake/commit", content=commit, headers={"x-aitp-session-id": sid})
    assert done.status_code == 200, done.text
    replay = await b.post("/aitp/handshake/commit", content=commit, headers={"x-aitp-session-id": sid})

    assert replay.status_code == 400
    assert replay.json()["code"] == "REPLAY_DETECTED"


async def test_a_stale_hello_is_refused_without_reaching_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    server, b = _responder()
    hello = _initiator(server).build_hello(server.manifest_json, [GRANT])
    monkeypatch.setattr(server.replay_guard, "_clock", lambda: time.time() + 301)
    monkeypatch.setattr(server, "_new_responder", lambda: pytest.fail("reached the SDK"))

    resp = await b.post("/aitp/handshake/hello", content=hello)

    assert resp.status_code == 400
    assert resp.json()["code"] == "TIMESTAMP_EXPIRED"
