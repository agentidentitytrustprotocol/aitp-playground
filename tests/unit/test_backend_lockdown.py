"""Backend lockdown: the API-key gate behind AITP_PLAYGROUND_REQUIRE_API_KEY,
refuse-to-start, and trusted client-IP resolution (src/aitp_playground/security.py).
"""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aitp_playground.config import Settings
from aitp_playground.main import create_app
from aitp_playground.security import (
    ClientResolution,
    fingerprint,
    is_direct_loopback,
    normalize_client_ip,
    parse_client_ip,
    resolve_client,
)
from tests._auth import TEST_API_KEY

KEY = TEST_API_KEY
XKEY = {"x-api-key": KEY}
BEARER = {"Authorization": f"Bearer {KEY}"}
LOOPBACK = ("127.0.0.1", 5000)
REMOTE = ("203.0.113.5", 5000)


def _locked(**kw) -> TestClient:
    kw.setdefault("client", REMOTE)
    return TestClient(create_app(Settings(aitp_playground_require_api_key=True)), **kw)


def _open(**kw) -> TestClient:
    kw.setdefault("client", REMOTE)
    return TestClient(create_app(Settings(aitp_playground_require_api_key=False)), **kw)


# --- refuse to start ---------------------------------------------------------

@pytest.mark.parametrize("key", ["", "   ", " , "])
def test_required_key_without_a_configured_key_refuses_to_start(key: str) -> None:
    with pytest.raises(ValueError, match="AITP_PLAYGROUND_REQUIRE_API_KEY"):
        Settings(aitp_playground_require_api_key=True, playground_api_key=key)


def test_flag_reads_its_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITP_PLAYGROUND_REQUIRE_API_KEY", "true")
    assert Settings().require_api_key is True
    monkeypatch.setenv("AITP_PLAYGROUND_REQUIRE_API_KEY", "false")
    assert Settings().require_api_key is False


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AITP_PLAYGROUND_REQUIRE_API_KEY", raising=False)
    assert Settings(_env_file=None).require_api_key is False


def test_bare_require_api_key_env_does_not_enable_the_lockdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AITP_PLAYGROUND_REQUIRE_API_KEY", raising=False)
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    assert Settings(_env_file=None).require_api_key is False


def test_flag_env_unset_with_no_key_still_starts() -> None:
    assert Settings(aitp_playground_require_api_key=False, playground_api_key="").require_api_key is False


# --- flag true: the gate -----------------------------------------------------

LOCKED_PATHS = [
    ("GET", "/capabilities"),
    ("GET", "/scenarios"),
    ("GET", "/packs"),
    ("GET", "/dashboard"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/runs"),
    ("GET", "/runs/some-run/events"),
    ("POST", "/runs"),
    ("GET", "/no/such/path"),
    ("GET", "/webhooks/cp/run-1"),  # only POST is HMAC-authenticated
    ("POST", "/webhooks/cp/a/b"),
]


@pytest.mark.parametrize("method,path", LOCKED_PATHS)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-api-key": "wrong"},
        {"x-api-key": ""},
        {"x-api-key": "   "},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic " + KEY},
        {"Authorization": f"Bearer {KEY}, other"},
        {"x-api-key": f"{KEY},other"},
    ],
    ids=["none", "wrong", "empty", "blank", "bearer-wrong", "basic", "bearer-list", "xkey-list"],
)
def test_locked_rejects_keyless_and_wrong_keys_on_every_path(
    method: str, path: str, headers: dict[str, str]
) -> None:
    r = _locked().request(method, path, headers=headers)
    assert r.status_code == 401, (method, path, r.status_code)
    assert r.headers["www-authenticate"] == 'ApiKey header="x-api-key"'
    assert r.json()["code"] == "unauthorized"


@pytest.mark.parametrize("headers", [XKEY, BEARER], ids=["x-api-key", "bearer"])
def test_locked_accepts_both_header_forms(headers: dict[str, str]) -> None:
    c = _locked()
    for path in ("/capabilities", "/scenarios", "/runs", "/openapi.json"):
        assert c.get(path, headers=headers).status_code == 200, path
    # An unknown path gets past the gate to the router's 404.
    assert c.get("/no/such/path", headers=headers).status_code == 404


def test_locked_accepts_any_key_in_the_rotation_list() -> None:
    c = TestClient(
        create_app(Settings(aitp_playground_require_api_key=True, playground_api_key="old, new")), client=REMOTE
    )
    for key in ("old", "new"):
        assert c.get("/capabilities", headers={"x-api-key": key}).status_code == 200
    assert c.get("/capabilities", headers={"x-api-key": "old, new"}).status_code == 401


def test_a_valid_key_in_either_header_is_enough() -> None:
    c = _locked()
    r = c.get("/runs", headers={"x-api-key": "wrong", "Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    r = c.get("/runs", headers={"x-api-key": KEY, "Authorization": "Bearer wrong"})
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_locked_healthz_stays_open(method: str) -> None:
    r = _locked().request(method, "/healthz")
    # GET is the health check; HEAD is not routed (405) today, gated or not.
    assert r.status_code == (200 if method == "GET" else 405)


def test_locked_options_is_never_gated() -> None:
    r = _locked().options("/runs")
    assert r.status_code == 405  # the router's answer, not the gate's 401


def test_locked_webhook_reaches_its_hmac_check() -> None:
    r = _locked().post("/webhooks/cp/unknown-run", content=b"{}")
    # The handler's own outcome (410: unknown run, so the CP stops), not the gate's.
    assert r.status_code == 410


def test_locked_telemetry_from_loopback_reaches_its_token_check() -> None:
    c = _locked(client=LOOPBACK)
    token = {"Authorization": f"Bearer {c.app.state.telemetry_token}"}
    r = c.post("/internal/telemetry", headers=token, json={"type": "x"})
    assert r.status_code == 200
    # Still token-checked: the route's own 401 (Bearer challenge), not the gate's.
    r = c.post("/internal/telemetry", json={"type": "x"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "client,headers",
    [
        (REMOTE, {}),
        (LOOPBACK, {"X-Forwarded-For": "198.51.100.1"}),
        (LOOPBACK, {"Forwarded": "for=198.51.100.1"}),
        (("testclient", 5000), {}),
    ],
    ids=["remote-peer", "loopback-via-xff", "loopback-via-forwarded", "non-ip-peer"],
)
def test_locked_internal_from_anything_but_direct_loopback_is_401(client, headers) -> None:
    c = _locked(client=client)
    token = {"Authorization": f"Bearer {c.app.state.telemetry_token}", **headers}
    r = c.post("/internal/telemetry", headers=token, json={"type": "x"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'ApiKey header="x-api-key"'


def test_locked_websocket_without_key_is_closed_1008() -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with _locked().websocket_connect("/ws-anything"):
            pass
    assert exc.value.code == 1008


def test_locked_websocket_with_a_valid_key_passes_the_gate() -> None:
    # No websocket route exists: passing the gate means the router, not the
    # middleware, closes it (1000), never the gate's 1008.
    with pytest.raises(WebSocketDisconnect) as exc:
        with _locked().websocket_connect("/ws-anything", headers=XKEY):
            pass
    assert exc.value.code != 1008


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/healthz/"),
        ("GET", "//internal/telemetry"),
        ("POST", "//internal/telemetry"),
        ("POST", "/webhooks/cp/a%2Fb/extra"),
        ("POST", "/webhooks/cp/"),
        ("GET", "/webhooks/cp/run-1"),
        ("GET", "/internalx"),
    ],
)
def test_locked_exemption_lookalikes_are_gated(method: str, path: str) -> None:
    r = _locked().request(method, path)
    assert r.status_code == 401, (method, path)
    assert r.headers["www-authenticate"] == 'ApiKey header="x-api-key"'


def test_locked_encoded_slash_webhook_is_gated() -> None:
    # %2F is decoded into the ASGI path, so this is two segments and gated.
    r = _locked().post("/webhooks/cp/a%2Fb")
    assert r.status_code == 401


def test_duplicate_headers_use_the_first_value() -> None:
    c = TestClient(_probe_app(Settings()), client=REMOTE)
    good_first = c.get(
        "/_probe",
        headers=[
            ("x-api-key", KEY), ("x-api-key", "wrong"),
            ("X-AITP-Client-IP", "198.51.100.7"), ("X-AITP-Client-IP", "198.51.100.8"),
        ],
    ).json()
    assert good_first["key_fp"] == fingerprint(KEY.encode())
    assert good_first["subject_ip"] == "198.51.100.7"
    bad_first = c.get(
        "/_probe", headers=[("x-api-key", "wrong"), ("x-api-key", KEY)]
    ).json()
    assert bad_first["key_fp"] is None


def test_locked_rejection_logs_only_the_fact(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="aitp_playground.security")
    _locked().get("/runs", headers={"x-api-key": "a-wrong-secret-value"})
    assert "rejected API key" in caplog.text
    assert "a-wrong-secret-value" not in caplog.text


# --- flag false: today's behaviour ---------------------------------------------

def test_open_public_listings_need_no_key() -> None:
    c = _open()
    for path in ("/healthz", "/capabilities", "/packs", "/scenarios", "/dashboard"):
        assert c.get(path).status_code == 200, path


def test_open_guarded_routes_keep_their_route_level_401() -> None:
    c = _open()
    r = c.get("/runs")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    r = c.get("/runs", headers={"x-api-key": "wrong"})
    assert r.status_code == 401


@pytest.mark.parametrize("headers", [XKEY, BEARER], ids=["x-api-key", "bearer"])
def test_open_route_auth_accepts_both_header_forms(headers: dict[str, str]) -> None:
    assert _open().get("/runs", headers=headers).status_code == 200


def test_open_wrong_key_on_a_public_route_is_logged_and_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="aitp_playground.security")
    r = _open().get("/capabilities", headers={"x-api-key": "nope"})
    assert r.status_code == 200
    assert "rejected API key" in caplog.text


# --- client resolution ---------------------------------------------------------

def _probe_app(settings: Settings):
    app = create_app(settings)

    @app.get("/_probe")
    def probe(request: Request) -> dict:
        res = resolve_client(request)
        return {"key_fp": res.key_fp, "subject_ip": res.subject_ip, "trusted": res.trusted_header}

    return app


@pytest.mark.parametrize("locked", [False, True])
def test_client_ip_header_honoured_only_with_a_valid_key(locked: bool) -> None:
    c = TestClient(_probe_app(Settings(aitp_playground_require_api_key=locked)), client=REMOTE)
    keyed = c.get("/_probe", headers={**XKEY, "X-AITP-Client-IP": "198.51.100.7"}).json()
    assert keyed == {"key_fp": fingerprint(KEY.encode()), "subject_ip": "198.51.100.7",
                     "trusted": True}
    if not locked:
        spoof = c.get("/_probe", headers={"X-AITP-Client-IP": "198.51.100.7"}).json()
        assert spoof == {"key_fp": None, "subject_ip": None, "trusted": False}
        wrong = c.get(
            "/_probe", headers={"x-api-key": "wrong", "X-AITP-Client-IP": "198.51.100.7"}
        ).json()
        assert wrong["subject_ip"] is None


def test_keyed_request_without_the_header_has_no_subject_ip() -> None:
    c = TestClient(_probe_app(Settings()), client=REMOTE)
    body = c.get("/_probe", headers={**BEARER, "X-Forwarded-For": "198.51.100.9"}).json()
    assert body["key_fp"] == fingerprint(KEY.encode())
    assert body["subject_ip"] is None
    assert body["trusted"] is False


@pytest.mark.parametrize(
    "value,want",
    [
        ("198.51.100.7", "198.51.100.7"),
        (" 198.51.100.7 , 10.0.0.1", "198.51.100.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("fe80::1%eth0", "fe80:0:0:0::/64"),
        ("", None),
        ("   ", None),
        (" , 1.2.3.4", None),
        ("198.51.100.7:443", None),
        ("[2001:db8::1]", None),
        ("[2001:db8::1]:443", None),
        ("example.com", None),
        ("1.2.3", None),
        ("unknown", None),
        ("1" * 80, None),
        ("198.51.100.7%eth0", None),
        ("%eth0", None),
    ],
)
def test_parse_client_ip(value: str, want: str | None) -> None:
    assert parse_client_ip(value) == want
    c = TestClient(_probe_app(Settings()), client=REMOTE)
    body = c.get("/_probe", headers={**XKEY, "X-AITP-Client-IP": value}).json()
    assert body["subject_ip"] == want
    assert body["trusted"] is (want is not None)


# Every case in aitp-docs/mcp/tests/ratelimit.test.ts (normalizeClientIp + the
# clientKey IPv6 cases), so the two implementations agree on identities.
@pytest.mark.parametrize(
    "raw,want",
    [
        ("198.51.100.7", "198.51.100.7"),
        (" 198.51.100.7 ", "198.51.100.7"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("::FFFF:c633:6407", "198.51.100.7"),
        ("0:0:0:0:0:ffff:198.51.100.7", "198.51.100.7"),
        ("::1", "0:0:0:0::/64"),
        ("::", "0:0:0:0::/64"),
        ("2001:db8::", "2001:db8:0:0::/64"),
        ("2001:0db8:0000:0042:0000:8a2e:0370:7334", "2001:db8:0:42::/64"),
        ("64:ff9b::198.51.100.7", "64:ff9b:0:0::/64"),
        ("1:2:3:4:5:6:7::", "1:2:3:4::/64"),
        ("not-an-ip", "not-an-ip"),
        ("unknown", "unknown"),
        ("1.2.3", "1.2.3"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2:ffff:ffff:ffff:fffe", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("fe80::1%eth0", "fe80:0:0:0::/64"),
    ],
)
def test_normalize_client_ip_matches_aitp_docs(raw: str, want: str) -> None:
    assert normalize_client_ip(raw) == want


def test_log_client_ip_is_normalised_right_most_xff() -> None:
    from aitp_playground.api._deps import client_ip

    app = create_app(Settings())

    @app.get("/_ip")
    def ip(request: Request) -> dict:
        return {"ip": client_ip(request)}

    c = TestClient(app, client=("::ffff:192.0.2.1", 1))
    assert c.get("/_ip").json() == {"ip": "192.0.2.1"}
    r = c.get("/_ip", headers={"X-Forwarded-For": "1.1.1.1, 2001:db8:1:2:aaaa::1"})
    assert r.json() == {"ip": "2001:db8:1:2::/64"}


@pytest.mark.parametrize(
    "client,headers,want",
    [
        (("127.0.0.1", 1), [], True),
        (("127.5.6.7", 1), [], True),
        (("::1", 1), [], True),
        (("::ffff:127.0.0.1", 1), [], True),
        (("127.0.0.1", 1), [(b"x-forwarded-for", b"1.2.3.4")], False),
        (("127.0.0.1", 1), [(b"X-Forwarded-For", b"1.2.3.4")], False),
        (("127.0.0.1", 1), [(b"forwarded", b"for=1.2.3.4")], False),
        (("10.0.0.1", 1), [], False),
        (("testclient", 1), [], False),
        (None, [], False),
    ],
)
def test_is_direct_loopback(client, headers, want: bool) -> None:
    assert is_direct_loopback({"type": "http", "client": client, "headers": headers}) is want


def test_resolve_client_without_middleware_computes_fresh() -> None:
    from types import SimpleNamespace

    req = SimpleNamespace(
        state=SimpleNamespace(),
        scope={"type": "http", "headers": [(b"x-api-key", KEY.encode()),
                                            (b"x-aitp-client-ip", b"192.0.2.4")]},
        app=SimpleNamespace(state=SimpleNamespace(settings=Settings())),
    )
    res = resolve_client(req)
    assert res == ClientResolution(fingerprint(KEY.encode()), "192.0.2.4", True, True)


# --- bootstrap: agents reach the playground over loopback --------------------

def test_bootstrap_telemetry_url_is_the_internal_loopback_url() -> None:
    from aitp_playground.hosting.bootstrap import BootstrapBuilder
    from aitp_playground.registry.models import AgentSpec
    from aitp_playground.registry.service import RegistryService

    settings = Settings(
        playground_base_url="https://playground.example.com",
        playground_internal_url="http://127.0.0.1:8000/",
    )
    manifest = RegistryService(settings).get_agent_manifest("_shared/agents/researcher")
    bs = BootstrapBuilder(settings).build(
        run_id="run-abc",
        agent_spec=AgentSpec(id="researcher", ref="_shared/agents/researcher"),
        resolved_manifest=manifest,
        port=8100,
        peers={},
        inputs={},
    )
    assert bs["playground"]["telemetry_url"] == "http://127.0.0.1:8000/internal/telemetry"
    assert "playground.example.com" not in bs["playground"]["telemetry_url"]


def test_non_loopback_internal_url_is_warned(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="aitp_playground.main")
    create_app(Settings(playground_internal_url="https://playground.example.com"))
    assert "PLAYGROUND_INTERNAL_URL" in caplog.text
    caplog.clear()
    create_app(Settings())
    assert "PLAYGROUND_INTERNAL_URL" not in caplog.text


def test_internal_url_defaults_to_loopback_port_8000() -> None:
    assert Settings().playground_internal_url == "http://127.0.0.1:8000"
