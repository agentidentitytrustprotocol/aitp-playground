"""The verifier side of revocation: an agent enforces its own signed list.

An agent verifies the TCTs it issued, so the list it enforces is the one it
signs and serves (`revocation_publish.py`), verified through the SDK against
its own AID before a single entry is applied. What is pinned here:

* a pull's entries reach the deny-set, and a later pull replaces them
  (un-revocation works), while local revocations survive every refresh;
* a failed pull keeps the last list in force until its `expires_at`, and with
  nothing in force the posture is degraded, never "nothing revoked";
* a list in force that does not verify as ours is discarded, with
  `revocation.verify_failed` emitted even on a quiet poll (D-14);
* without a CP there is nothing to refresh, and the posture is unchecked.

`revocation_refresh.refresh_revocations` is the production ingest; only the
CP transport is replaced.
"""
from __future__ import annotations

import json
import sys
import time
import types
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import revocation_publish  # noqa: E402
import revocation_refresh  # noqa: E402
from revocation_publish import IssuerRevocationList  # noqa: E402
from revocation_state import RevocationState  # noqa: E402

JTI_A = str(uuid.uuid4())
JTI_B = str(uuid.uuid4())


class FakeCp:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        return httpx.Response(
            200, json={"iss": request.url.params["iss"], "entries": self.entries},
        )


@pytest.fixture
def cp(monkeypatch: pytest.MonkeyPatch) -> FakeCp:
    fake = FakeCp()

    def factory(**_kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(
        revocation_publish, "httpx",
        types.SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError),
    )
    return fake


class Harness:
    def __init__(self, *, cp: bool = True, ttl: int = 3600) -> None:
        self.agent = aitp.AitpAgent.generate()
        self.bootstrap: dict[str, Any] = {"run_id": "r", "agent_id": "b"}
        if cp:
            self.bootstrap["cp"] = {"base_url": "http://cp", "list_ttl_secs": ttl}
        self.state = RevocationState()
        self.issued = IssuerRevocationList(
            agent_provider=lambda: self.agent, bootstrap=self.bootstrap,
        )
        self.events: list[dict[str, Any]] = []

    async def _emit(self, event_type: str, _bootstrap: dict, **fields: Any) -> None:
        self.events.append({"type": event_type, **fields})

    async def refresh(self, *, quiet: bool = False) -> dict[str, Any]:
        return await revocation_refresh.refresh_revocations(
            revocation=self.state, issued=self.issued, bootstrap=self.bootstrap,
            emit=self._emit, quiet=quiet,
        )

    def posture(self, now: float | None = None) -> str:
        return self.state.posture(can_verify=True, max_staleness_secs=300, now=now)


# ── entries reach the deny-set ──────────────────────────────────────────


async def test_pulled_entries_are_verified_and_enforced(cp: FakeCp) -> None:
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    h = Harness()

    result = await h.refresh()

    assert result["verified"] is True and result["published"] is True
    assert h.state.is_revoked(JTI_A) and not h.state.is_locally_revoked(JTI_A)
    assert h.posture() == "current"
    fetched = [e for e in h.events if e["type"] == "revocation.list_fetched"]
    assert fetched and fetched[0]["issuer"] == h.agent.aid


async def test_a_later_pull_replaces_the_deny_set_so_un_revocation_works(cp: FakeCp) -> None:
    h = Harness()
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await h.refresh()
    cp.entries = [{"jti": JTI_B, "revoked_at": 2}]
    await h.refresh()

    assert h.state.effective_jtis == {JTI_B}


async def test_a_refresh_never_clears_a_local_revocation(cp: FakeCp) -> None:
    h = Harness()
    h.state.revoke_local(JTI_A)
    await h.refresh()

    assert h.state.effective_jtis == {JTI_A}


async def test_an_empty_answer_is_an_assertion_not_a_gap(cp: FakeCp) -> None:
    h = Harness()

    assert (await h.refresh())["verified"] is True
    assert h.state.effective_jtis == set()
    assert h.posture() == "current"


# ── the CP is unavailable ───────────────────────────────────────────────


async def test_a_failed_pull_keeps_the_last_list_in_force_until_it_expires(cp: FakeCp) -> None:
    h = Harness(ttl=60)
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await h.refresh()
    cp.down = True

    result = await h.refresh()

    assert result["published"] is False
    assert h.state.is_revoked(JTI_A)
    assert h.posture() == "current"
    assert h.posture(now=time.time() + 61) == "degraded"


async def test_never_pulled_is_degraded_not_nothing_revoked(cp: FakeCp) -> None:
    cp.down = True
    h = Harness()

    result = await h.refresh()

    assert result["published"] is False and "verified" not in result
    assert h.state.snapshot is None
    assert h.posture() == "degraded"


# ── a list that is not ours ─────────────────────────────────────────────


async def test_a_list_in_force_that_does_not_verify_is_discarded_even_quietly(
    cp: FakeCp, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness()
    cp.entries = [{"jti": JTI_A, "revoked_at": 1}]
    await h.refresh()
    good = h.issued.current
    assert good is not None

    # Tamper with the signed body: the SDK must refuse it, so nothing applies.
    envelope = json.loads(good.envelope_json)
    envelope["revocation_list"]["entries"] = []
    tampered = replace(good, envelope_json=json.dumps(envelope))
    cp.down = True  # keep the pull from re-signing over the tampered list
    h.issued.current = tampered
    h.events.clear()

    result = await h.refresh(quiet=True)

    assert result["discarded"]
    assert h.state.is_revoked(JTI_A), "the previously applied list stays in force"
    assert [e["type"] for e in h.events] == ["revocation.verify_failed"]


# ── no control plane ────────────────────────────────────────────────────


async def test_without_a_cp_there_is_nothing_to_refresh() -> None:
    h = Harness(cp=False)
    h.state.revoke_local(JTI_A)

    result = await h.refresh()

    assert result["skipped"] == "no cp configured"
    assert h.state.posture(can_verify=False, max_staleness_secs=300) == "unchecked"
    assert h.state.effective_jtis == {JTI_A}
