"""Downstream PoP on capability calls (RFC-AITP-0005 §6, §10.5; PGP.1 part 2, #105).

A TCT presented back to its issuer is accepted only with a fresh proof that
the caller holds `sub`'s key. The first call gets a 401 carrying
`x-aitp-pop-challenge`; the retry answers it in `x-aitp-pop-response`. Each
challenge is answered at most once.

The route tests drive a real `AitpServer` with a TCT a real handshake minted,
and real SDK challenges and responses.
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
from fastapi import FastAPI, Request

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import aitp_server  # noqa: E402
import bootstrap as agent_bootstrap  # noqa: E402
from aitp_server import AitpServer  # noqa: E402
from pop_challenges import (  # noqa: E402
    CHALLENGE_HEADER, RESPONSE_HEADER, PopChallenges, PopRejected, decode_header, encode_header,
)
from revocation_state import RevocationState  # noqa: E402
from tct_claims import decode_claims  # noqa: E402

GRANT = "demo.write"


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def _challenge(nonce: str, expires_at: float) -> str:
    return json.dumps({"tct_jti": str(uuid.uuid4()), "nonce": nonce, "expires_at": int(expires_at)})


# ── the store ───────────────────────────────────────────────────────────


def test_a_challenge_is_consumed_by_its_first_answer() -> None:
    store = PopChallenges()
    issued = store.issue(lambda _ttl: _challenge("n1", time.time() + 60))

    assert store.consume(json.dumps({"nonce_echo": "n1"})) == issued
    with pytest.raises(PopRejected) as exc:
        store.consume(json.dumps({"nonce_echo": "n1"}))
    assert exc.value.code == "POP_CHALLENGE_INVALID"


def test_an_expired_challenge_is_forgotten() -> None:
    clock = Clock()
    store = PopChallenges(clock=clock)
    store.issue(lambda _ttl: _challenge("n1", clock.now + 60))

    clock.now += 61
    with pytest.raises(PopRejected) as exc:
        store.consume(json.dumps({"nonce_echo": "n1"}))
    assert exc.value.code == "POP_CHALLENGE_INVALID"
    assert store._outstanding == {}


def test_the_ttl_is_handed_to_the_sdk() -> None:
    seen: list[int] = []
    PopChallenges(ttl_secs=17).issue(lambda ttl: seen.append(ttl) or _challenge("n", time.time() + ttl))
    assert seen == [17]


@pytest.mark.parametrize("body", ["not json", "[]", "{}", json.dumps({"nonce_echo": ""})])
def test_a_response_naming_no_nonce_is_an_invalid_response(body: str) -> None:
    with pytest.raises(PopRejected) as exc:
        PopChallenges().consume(body)
    assert exc.value.code == "POP_RESPONSE_INVALID"


def test_a_full_store_refuses_instead_of_forgetting() -> None:
    store = PopChallenges(max_entries=2)
    store.issue(lambda _ttl: _challenge("a", time.time() + 60))
    store.issue(lambda _ttl: _challenge("b", time.time() + 60))

    with pytest.raises(PopRejected) as exc:
        store.issue(lambda _ttl: _challenge("c", time.time() + 60))
    assert (exc.value.code, exc.value.status) == (None, 429)


def test_header_encoding_is_unpadded_base64url() -> None:
    payload = json.dumps({"k": "v?>"})
    encoded = encode_header(payload)
    assert "=" not in encoded and "+" not in encoded and "/" not in encoded
    assert decode_header(encoded) == payload


# ── the route ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    async def emit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(aitp_server, "emit_event", emit)


async def _issuer_and_holder() -> tuple[AitpServer, httpx.AsyncClient, "aitp.AitpAgent", str]:
    """B serves one capability; A completes a handshake with B and holds the
    TCT B issued it."""
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

    @app.post(f"/capabilities/{GRANT}")
    async def capability(request: Request) -> dict[str, Any]:
        identity = server.verify_capability_tct(
            request.headers.get("x-aitp-tct", ""), GRANT,
            pop_response=request.headers.get("x-aitp-pop-response", ""),
        )
        return {"ok": True, "grants": list(identity.grants)}

    b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b")

    holder = aitp.AitpAgent.generate()
    holder.build_manifest(display_name="a", handshake_endpoint="https://a/x", offered_caps=["demo.read"])
    server.trusted_peer_aids.add(holder.aid)
    session = holder.new_session(pinned_keys=[agent.aid])
    ack = await b.post("/aitp/handshake/hello", content=session.build_hello(server.manifest_json, [GRANT]))
    sid = ack.headers["x-aitp-session-id"]
    done = await b.post(
        "/aitp/handshake/commit", content=session.process_hello_ack(ack.text, sid),
        headers={"x-aitp-session-id": sid},
    )
    assert done.status_code == 200, done.text
    tct = json.loads(session.complete(done.text, revoked_jtis=set(), revocation="fail_closed"))["tct"]
    return server, b, holder, tct


async def _challenged(b: httpx.AsyncClient, tct: str) -> str:
    r = await b.post(f"/capabilities/{GRANT}", headers={"x-aitp-tct": tct})
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "POP_RESPONSE_INVALID"
    return decode_header(r.headers[CHALLENGE_HEADER])


async def _answer(b: httpx.AsyncClient, tct: str, response_json: str) -> httpx.Response:
    return await b.post(f"/capabilities/{GRANT}", headers={
        "x-aitp-tct": tct, RESPONSE_HEADER: encode_header(response_json),
    })


async def test_a_genuine_tct_without_pop_is_challenged_then_a_valid_answer_is_accepted() -> None:
    server, b, holder, tct = await _issuer_and_holder()

    challenge = await _challenged(b, tct)
    assert json.loads(challenge)["tct_jti"] == decode_claims(tct)["jti"]

    r = await _answer(b, tct, holder.sign_pop_response(challenge))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "grants": [GRANT]}


async def test_an_answer_signed_by_a_key_other_than_sub_is_refused() -> None:
    _server, b, _holder, tct = await _issuer_and_holder()
    thief = aitp.AitpAgent.generate()

    r = await _answer(b, tct, thief.sign_pop_response(await _challenged(b, tct)))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "POP_RESPONSE_INVALID"


async def test_a_reused_nonce_is_refused() -> None:
    _server, b, holder, tct = await _issuer_and_holder()
    response = holder.sign_pop_response(await _challenged(b, tct))

    assert (await _answer(b, tct, response)).status_code == 200
    replay = await _answer(b, tct, response)
    assert replay.status_code == 403, replay.text
    assert replay.json()["detail"]["code"] == "POP_CHALLENGE_INVALID"


async def test_an_expired_nonce_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    server, b, holder, tct = await _issuer_and_holder()
    response = holder.sign_pop_response(await _challenged(b, tct))
    monkeypatch.setattr(server.pop_challenges, "_clock", lambda: time.time() + 61)

    r = await _answer(b, tct, response)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "POP_CHALLENGE_INVALID"


async def test_a_nonce_the_issuer_never_sent_is_refused() -> None:
    server, b, holder, tct = await _issuer_and_holder()
    # A well-formed challenge for this TCT that B never issued.
    forged = server.agent.new_pop_challenge(tct, 60)

    r = await _answer(b, tct, holder.sign_pop_response(forged))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "POP_CHALLENGE_INVALID"


async def test_a_failed_answer_still_consumes_the_challenge() -> None:
    """At most one answer per challenge, valid or not (RFC-AITP-0001 §5.5)."""
    _server, b, holder, tct = await _issuer_and_holder()
    challenge = await _challenged(b, tct)

    bad = await _answer(b, tct, aitp.AitpAgent.generate().sign_pop_response(challenge))
    assert bad.status_code == 403
    late = await _answer(b, tct, holder.sign_pop_response(challenge))
    assert late.status_code == 403
    assert late.json()["detail"]["code"] == "POP_CHALLENGE_INVALID"


async def test_a_challenge_for_another_tct_does_not_answer_this_one() -> None:
    """The response is bound to the challenged TCT's jti; the SDK refuses it
    for any other."""
    server, b, holder, tct = await _issuer_and_holder()
    _server2, _b2, _holder2, other_tct = await _issuer_and_holder()
    other_challenge = server.pop_challenges.issue(
        lambda ttl: server.agent.new_pop_challenge(other_tct, ttl),
    )

    r = await _answer(b, tct, holder.sign_pop_response(other_challenge))
    assert r.status_code == 403, r.text


async def test_pre_checks_still_refuse_before_any_challenge_is_issued() -> None:
    """A missing token, a revoked jti and a foreign issuer stay plain 403s
    and open no challenge."""
    server, b, _holder, tct = await _issuer_and_holder()
    _other, _b2, _h2, foreign = await _issuer_and_holder()

    missing = await b.post(f"/capabilities/{GRANT}")
    assert missing.status_code == 403
    alien = await b.post(f"/capabilities/{GRANT}", headers={"x-aitp-tct": foreign})
    assert alien.status_code == 403
    assert "issuer mismatch" in alien.json()["detail"]
    server.revocation.revoke_local(decode_claims(tct)["jti"])
    revoked = await b.post(f"/capabilities/{GRANT}", headers={"x-aitp-tct": tct})
    assert revoked.status_code == 403
    assert "tct revoked (local)" in revoked.json()["detail"]

    assert server.pop_challenges._outstanding == {}
