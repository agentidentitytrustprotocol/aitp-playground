"""#117 Phase 2: agent routes refuse a missing SDK feature with a coded 501.

aitp-sdk 0.23 has every feature, so absence is simulated by deleting the
attribute (``monkeypatch.delattr(..., raising=False)``: on a genuinely
stable-only wheel it is already gone, and these tests still hold).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_AGENT_BASE = Path(__file__).resolve().parents[2] / "agents" / "base"
if str(_AGENT_BASE) not in sys.path:
    sys.path.insert(0, str(_AGENT_BASE))

import aitp  # noqa: E402

import agent_admin  # noqa: E402
import sdk_features  # noqa: E402
from agent_admin import build_admin_router  # noqa: E402
from revocation_state import RevocationState  # noqa: E402
from aitp_playground import capabilities as caps  # noqa: E402
from tests.unit.test_agent_admin_routes import _participant_handshake  # noqa: E402
from tests.unit.test_delegation_revocation import (  # noqa: E402
    _client as _server_client,
    _delegation_for,
    _redeem,
    _server,
)

# Every attribute a gated feature needs, by feature, with its owner.
_ATTRS: dict[str, list[tuple[Any, str]]] = {
    sdk_features.TCT_RENEWAL: [
        (aitp.AitpAgent, "build_renewal_request"),
        (aitp.AitpAgent, "process_renewal_request"),
    ],
    sdk_features.SESSION_BUNDLE: [
        (aitp, "SessionBundleBuilder"),
        (aitp, "verify_session_bundle"),
    ],
    sdk_features.MULTIHOP_DELEGATION: [(aitp, "verify_delegation_multihop")],
}


def _remove(monkeypatch, feature: str) -> None:
    for owner, name in _ATTRS[feature]:
        monkeypatch.delattr(owner, name, raising=False)
    assert not sdk_features.has(feature)


def _assert_501(resp: httpx.Response | Any, feature: str) -> None:
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body["code"] == "SDK_FEATURE_UNAVAILABLE"
    assert body["feature"] == feature
    assert feature in body["error"]


def _app(**kwargs: Any) -> tuple[FastAPI, Any]:
    agent = kwargs.pop("agent", None) or aitp.AitpAgent.generate()
    app = FastAPI()
    app.include_router(build_admin_router(
        agent=agent,
        bootstrap={"run_id": "r", "agent_id": "a", "aitp": {}},
        held_tcts=kwargs.pop("held_tcts", {}),
        revocation=RevocationState(),
        **kwargs,
    ))
    return app, agent


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _spy_httpx(monkeypatch, handler) -> list[httpx.Request]:
    """Route agent_admin's httpx through ``handler``; returns the call log."""
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        agent_admin, "httpx",
        types.SimpleNamespace(
            AsyncClient=lambda *_a, **_k: httpx.AsyncClient(
                transport=httpx.MockTransport(recording),
            ),
            HTTPError=httpx.HTTPError,
            HTTPStatusError=httpx.HTTPStatusError,
        ),
    )
    return calls


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no network call expected, got {request.url}")


# ── has() / unavailable() ───────────────────────────────────────────────────


@pytest.mark.parametrize("feature", list(sdk_features.FEATURES))
def test_has_needs_every_name(monkeypatch, feature) -> None:
    owner, name = _ATTRS[feature][-1]
    monkeypatch.delattr(owner, name, raising=False)
    assert sdk_features.has(feature) is False


def test_has_is_false_without_the_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "aitp", None)
    assert not any(sdk_features.has(f) for f in sdk_features.FEATURES)


def test_unknown_feature_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        sdk_features.has("renewal")
    with pytest.raises(KeyError):
        sdk_features.unavailable("renewal")


def test_unavailable_body() -> None:
    resp = sdk_features.unavailable(sdk_features.TCT_RENEWAL)
    assert resp.status_code == 501
    body = json.loads(resp.body)
    assert body == {
        "error": f"aitp-sdk {sdk_features.sdk_version()} lacks tct_renewal",
        "code": "SDK_FEATURE_UNAVAILABLE",
        "feature": "tct_renewal",
    }


@pytest.mark.parametrize("deleted", [None, *sdk_features.FEATURES])
def test_probe_parity_with_the_playground(monkeypatch, deleted) -> None:
    """The agent probe and the engine's probe agree, feature by feature,
    on the real wheel and with each feature's last attribute deleted."""
    if deleted is not None:
        owner, name = _ATTRS[deleted][-1]
        monkeypatch.delattr(owner, name, raising=False)
    caps.get_capabilities.cache_clear()
    try:
        for feature in sdk_features.FEATURES:
            assert sdk_features.has(feature) == caps.has_feature(feature), feature
    finally:
        caps.get_capabilities.cache_clear()


def test_feature_keys_match_the_playground_keys() -> None:
    assert set(sdk_features.FEATURES) <= set(caps.ALL_FEATURES)


# ── /admin/renew-tct (holder) ───────────────────────────────────────────────


def test_renew_tct_without_renewal_is_501(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.TCT_RENEWAL)
    calls = _spy_httpx(monkeypatch, _unreachable)
    app, _ = _app(held_tcts={9999: "held.tct.token"})
    _assert_501(_client(app).post("/admin/renew-tct", json={"peer_port": 9999}), "tct_renewal")
    assert calls == []


def test_renew_tct_without_a_session_is_still_412(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.TCT_RENEWAL)
    app, _ = _app()
    resp = _client(app).post("/admin/renew-tct", json={"peer_port": 9999})
    assert resp.status_code == 412, resp.text


def test_issuer_501_reaches_the_holder_unchanged(monkeypatch) -> None:
    """The holder's wheel can renew, the issuer's can't: the issuer's coded
    refusal comes back through the holder, not as a bare 500."""
    coord = aitp.AitpAgent.generate()
    holder = aitp.AitpAgent.generate()
    coord_manifest = coord.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    holder.build_manifest("h", "http://h/aitp/handshake/hello", ["x"])
    tct = _participant_handshake(holder, coord, coord_manifest)

    issuer_body = {
        "error": "aitp-sdk 0.24.0 lacks tct_renewal",
        "code": "SDK_FEATURE_UNAVAILABLE",
        "feature": "tct_renewal",
    }
    calls = _spy_httpx(monkeypatch, lambda _r: httpx.Response(501, json=issuer_body))
    app, _ = _app(agent=holder, held_tcts={9999: tct})
    resp = _client(app).post("/admin/renew-tct", json={"peer_port": 9999})
    assert resp.status_code == 501, resp.text
    assert resp.json() == issuer_body
    assert [c.url.path for c in calls] == ["/admin/process-renewal"]


def test_other_issuer_errors_still_raise(monkeypatch) -> None:
    """Only the coded 501 is relayed; an issuer 409 keeps its pre-existing handling."""
    coord = aitp.AitpAgent.generate()
    holder = aitp.AitpAgent.generate()
    coord_manifest = coord.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    holder.build_manifest("h", "http://h/aitp/handshake/hello", ["x"])
    tct = _participant_handshake(holder, coord, coord_manifest)
    _spy_httpx(monkeypatch, lambda _r: httpx.Response(409, json={"detail": "replay"}))
    app, _ = _app(agent=holder, held_tcts={9999: tct})
    resp = _client(app).post("/admin/renew-tct", json={"peer_port": 9999})
    assert resp.status_code == 500


# ── /admin/process-renewal (issuer) ─────────────────────────────────────────


@pytest.mark.parametrize("body", [b'{"renewal": "request"}', b"not json at all"])
def test_process_renewal_without_renewal_is_501_first(monkeypatch, body) -> None:
    _remove(monkeypatch, sdk_features.TCT_RENEWAL)
    app, _ = _app()
    resp = _client(app).post("/admin/process-renewal", content=body)
    _assert_501(resp, "tct_renewal")


# ── /admin/export-session-bundle ────────────────────────────────────────────


def test_export_session_bundle_without_bundles_is_501(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.SESSION_BUNDLE)
    app, _ = _app(issued_tcts={"aid:participant": "issued.tct.token"})
    _assert_501(_client(app).post("/admin/export-session-bundle", json={}), "session_bundle")


def test_export_session_bundle_without_participants_is_still_412(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.SESSION_BUNDLE)
    app, _ = _app()
    resp = _client(app).post("/admin/export-session-bundle", json={})
    assert resp.status_code == 412, resp.text


# ── /admin/verify-session-bundle ────────────────────────────────────────────


def test_verify_session_bundle_without_bundles_is_501_and_fetches_nothing(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.SESSION_BUNDLE)
    calls = _spy_httpx(monkeypatch, _unreachable)
    app, _ = _app()
    resp = _client(app).post("/admin/verify-session-bundle", json={
        "bundle_envelope": "{}",
        "coordinator_manifest_url": "http://coord/.well-known/aitp-manifest",
    })
    _assert_501(resp, "session_bundle")
    assert calls == []


def test_verify_session_bundle_without_a_manifest_url_is_still_400(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.SESSION_BUNDLE)
    app, _ = _app()
    resp = _client(app).post("/admin/verify-session-bundle", json={"bundle_envelope": "{}"})
    assert resp.status_code == 400, resp.text


# ── /aitp/delegation/redeem multihop branch ─────────────────────────────────


def test_multihop_redeem_without_multihop_is_501(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.MULTIHOP_DELEGATION)
    state = RevocationState()
    server, manifest = _server(state)
    server.bootstrap["aitp"]["allow_multihop_delegation"] = True
    token, issuer = _delegation_for(server, manifest)
    _assert_501(_redeem(_server_client(server), token, issuer), "multihop_delegation")


def test_single_hop_redeem_ignores_a_missing_multihop(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.MULTIHOP_DELEGATION)
    state = RevocationState()
    server, manifest = _server(state)
    token, issuer = _delegation_for(server, manifest)
    resp = _redeem(_server_client(server), token, issuer)
    assert resp.status_code == 200, resp.text


def test_multihop_guard_runs_after_the_body_check(monkeypatch) -> None:
    _remove(monkeypatch, sdk_features.MULTIHOP_DELEGATION)
    server, _ = _server(RevocationState())
    server.bootstrap["aitp"]["allow_multihop_delegation"] = True
    resp = _server_client(server).post(
        "/aitp/delegation/redeem", json={"delegation_token": "t", "issuer_manifests": []},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("issuer", [
    httpx.Response(501, text="<html>Not Implemented</html>"),
    httpx.Response(501, json={"code": "SOMETHING_ELSE"}),
])
def test_an_uncoded_issuer_501_is_not_relayed(monkeypatch, issuer) -> None:
    """Only the SDK refusal is relayed; a proxy's 501 stays a holder 500."""
    coord = aitp.AitpAgent.generate()
    holder = aitp.AitpAgent.generate()
    coord_manifest = coord.build_manifest(
        "coord", "http://coord/aitp/handshake/hello", ["session.member"],
    )
    holder.build_manifest("h", "http://h/aitp/handshake/hello", ["x"])
    tct = _participant_handshake(holder, coord, coord_manifest)
    _spy_httpx(monkeypatch, lambda _r: issuer)
    app, _ = _app(agent=holder, held_tcts={9999: tct})
    resp = _client(app).post("/admin/renew-tct", json={"peer_port": 9999})
    assert resp.status_code == 500
