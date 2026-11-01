"""Scenario agents on loopback: TLS by default, plain HTTP only by local opt-in.

aitp-sdk >= 0.25 refuses a non-https `handshake_endpoint`. These tests hold the
default refusal, the trust scope of the loopback certificate, and the refusal
to run the opt-in on Railway. The TLS tests use real sockets and handshakes.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import ssl
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import aitp
import httpx
import pytest
from loopback import LoopbackClients, insecure_localhost

from aitp_playground.config import RAILWAY_PLATFORM_VARS, Settings
from aitp_playground.hosting.bootstrap import BootstrapBuilder
from aitp_playground.hosting.loopback import AgentLoopback, mint_loopback_cert
from aitp_playground.registry.models import AgentSpec
from aitp_playground.registry.service import RegistryService

ROOT = Path(__file__).resolve().parents[2]
HTTP_LOOPBACK = "http://localhost:8100/aitp/handshake/hello"
HTTPS_LOOPBACK = "https://localhost:8100/aitp/handshake/hello"
CAP = "demo.write"


def _settings(**kwargs) -> Settings:
    return Settings(cp_base_url="", cp_api_key="", **kwargs)


def _bootstrap(settings: Settings, **kwargs) -> dict:
    manifest = RegistryService(settings).get_agent_manifest("_shared/agents/analyzer")
    return BootstrapBuilder(settings).build(
        run_id="r",
        agent_spec=AgentSpec(id="analyzer", ref="_shared/agents/analyzer"),
        resolved_manifest=manifest,
        port=8100,
        peers={},
        inputs={},
        **kwargs,
    )


def _manifest(agent: "aitp.AitpAgent", endpoint: str) -> str:
    return agent.build_manifest(display_name="x", handshake_endpoint=endpoint, offered_caps=[CAP])


# ── the SDK's default refuses http:// ──────────────────────────────────────


def test_default_verification_refuses_http_loopback() -> None:
    manifest = _manifest(aitp.AitpAgent.generate(), HTTP_LOOPBACK)
    with pytest.raises(aitp.ManifestVerificationError) as err:
        aitp.verify_manifest_json(manifest)
    assert err.value.code == "malformed"


def test_default_build_hello_refuses_http_loopback() -> None:
    a, b = aitp.AitpAgent.generate(), aitp.AitpAgent.generate()
    _manifest(a, HTTP_LOOPBACK)
    peer = _manifest(b, HTTP_LOOPBACK)
    with pytest.raises(RuntimeError) as err:
        a.new_session(pinned_keys=[b.aid]).build_hello(peer, [CAP])
    assert err.value.code == "MANIFEST_INVALID"


def test_default_process_hello_refuses_http_loopback() -> None:
    a, b = aitp.AitpAgent.generate(), aitp.AitpAgent.generate()
    _manifest(a, HTTP_LOOPBACK)
    peer = _manifest(b, HTTP_LOOPBACK)
    hello = a.new_session(pinned_keys=[b.aid], insecure_localhost_dangerous=True).build_hello(
        peer, [CAP]
    )
    with pytest.raises(RuntimeError) as err:
        b.new_responder(pinned_keys=[a.aid]).process_hello(hello)
    assert err.value.code == "MANIFEST_INVALID"


def test_the_opt_in_never_admits_a_host_off_loopback() -> None:
    manifest = _manifest(aitp.AitpAgent.generate(), "http://org-b.aitp.test:9100/aitp/handshake/hello")
    with pytest.raises(aitp.ManifestVerificationError):
        aitp.verify_manifest_json(manifest, insecure_localhost_dangerous=True)


def test_https_loopback_verifies_without_the_opt_in() -> None:
    aitp.verify_manifest_json(_manifest(aitp.AitpAgent.generate(), HTTPS_LOOPBACK))


# ── the default posture this service gives an agent ────────────────────────


def test_the_opt_in_is_off_by_default() -> None:
    assert Settings.model_fields["insecure_localhost_dangerous"].default is False


def test_default_bootstrap_is_tls_on_loopback_only() -> None:
    bs = _bootstrap(_settings())
    assert bs["aitp"]["handshake_endpoint"] == HTTPS_LOOPBACK
    assert bs["aitp"]["did_web_scheme"] == "https"
    assert bs["aitp"]["insecure_localhost_dangerous"] is False
    assert bs["listen_host"] == "127.0.0.1"
    assert insecure_localhost(bs) is False


def test_a_bootstrap_without_the_flag_is_not_opted_in() -> None:
    assert insecure_localhost({"aitp": {}}) is False


def test_key_material_is_private() -> None:
    tls = _bootstrap(_settings())["loopback_tls"]
    for path in (tls["certfile"], tls["keyfile"]):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(Path(tls["keyfile"]).parent).st_mode) == 0o700


def test_explicit_opt_in_is_plain_loopback_without_tls() -> None:
    bs = _bootstrap(_settings(insecure_localhost_dangerous=True))
    assert bs["aitp"]["handshake_endpoint"] == HTTP_LOOPBACK
    assert bs["loopback_tls"] is None
    assert insecure_localhost(bs) is True


def test_a_public_origin_listens_plainly_behind_its_terminator() -> None:
    bs = _bootstrap(_settings(), public_origin="https://org-b.aitp.test")
    assert bs["aitp"]["handshake_endpoint"] == "https://org-b.aitp.test/aitp/handshake/hello"
    assert bs["listen_host"] == "0.0.0.0"
    assert bs["loopback_tls"] is None


def test_run_agent_requires_the_tls_keyword() -> None:
    from aitp_server import run_agent

    param = inspect.signature(run_agent).parameters["tls"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize("agent", ["researcher", "writer", "analyzer"])
def test_entrypoints_take_their_bind_address_from_the_bootstrap(agent: str) -> None:
    source = (ROOT / "agents" / agent / "main.py").read_text()
    assert 'host=bootstrap["listen_host"]' in source
    assert "0.0.0.0" not in source


@pytest.mark.parametrize(
    "path",
    ["Dockerfile", "docker-compose.yml", "docker-compose.test.yml", "docker-compose.dev.yml",
     "federated/docker-compose.federated-tls.yml"],
)
def test_no_deployable_config_sets_the_opt_in(path: str) -> None:
    assert "INSECURE_LOCALHOST_DANGEROUS" not in (ROOT / path).read_text()


# ── Railway refuses the opt-in ─────────────────────────────────────────────


@pytest.fixture()
def off_railway(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in RAILWAY_PLATFORM_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("name", RAILWAY_PLATFORM_VARS)
def test_the_opt_in_refuses_to_load_on_railway(off_railway, name: str) -> None:
    off_railway.setenv(name, "some-id")
    with pytest.raises(ValueError, match="INSECURE_LOCALHOST_DANGEROUS"):
        _settings(insecure_localhost_dangerous=True)


@pytest.mark.parametrize("name", RAILWAY_PLATFORM_VARS)
def test_the_opt_in_from_the_environment_refuses_on_railway(off_railway, name: str) -> None:
    off_railway.setenv(name, "some-id")
    off_railway.setenv("INSECURE_LOCALHOST_DANGEROUS", "1")
    with pytest.raises(ValueError, match="INSECURE_LOCALHOST_DANGEROUS"):
        Settings()


def test_railway_without_the_opt_in_loads(off_railway) -> None:
    off_railway.setenv(RAILWAY_PLATFORM_VARS[0], "some-id")
    assert _settings().insecure_localhost_dangerous is False


def test_the_opt_in_loads_off_railway(off_railway) -> None:
    assert _settings(insecure_localhost_dangerous=True).insecure_localhost_dangerous is True


# ── trust scope, over real TLS ─────────────────────────────────────────────


@asynccontextmanager
async def _tls_server(certfile: str, keyfile: str):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=ctx)
    async with server:
        yield server.sockets[0].getsockname()[1]


def _clients() -> list:
    """The service's trust and an agent's trust, from one minted certificate."""
    service = AgentLoopback(_settings())
    return [service, LoopbackClients({"loopback_tls": service.agent_tls()})]


@pytest.mark.parametrize("which", [0, 1], ids=["service", "agent"])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
async def test_the_agent_client_accepts_the_minted_cert(which: int, host: str) -> None:
    service, agent = _clients()
    tls = service.agent_tls()
    async with _tls_server(tls["certfile"], tls["keyfile"]) as port:
        async with httpx.AsyncClient(mounts=[service, agent][which].mounts()) as client:
            assert (await client.get(f"https://{host}:{port}/")).text == "ok"


@pytest.mark.parametrize("which", [0, 1], ids=["service", "agent"])
async def test_the_agent_client_refuses_a_cert_from_another_root(which: int) -> None:
    service, agent = _clients()
    with tempfile.TemporaryDirectory() as other:
        certfile, keyfile = mint_loopback_cert(Path(other))
        async with _tls_server(str(certfile), str(keyfile)) as port:
            async with httpx.AsyncClient(mounts=[service, agent][which].mounts()) as client:
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                    await client.get(f"https://localhost:{port}/")


async def test_an_external_client_does_not_trust_the_cert() -> None:
    tls = AgentLoopback(_settings()).agent_tls()
    async with _tls_server(tls["certfile"], tls["keyfile"]) as port:
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(f"https://localhost:{port}/")


@pytest.mark.parametrize("which", [0, 1], ids=["service", "agent"])
@pytest.mark.parametrize(
    "url",
    ["https://example.com/", "https://localhost.example.com/", "https://127.0.0.1.example.com/"],
)
async def test_other_hosts_on_an_agent_client_stay_on_default_trust(which: int, url: str) -> None:
    service, agent = _clients()
    source = [service, agent][which]
    async with httpx.AsyncClient(mounts=source.mounts()) as client:
        assert client._transport_for_url(httpx.URL(url)) is client._transport


def test_minting_sets_no_process_wide_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    service, _ = _clients()
    service.mounts()
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        assert name not in os.environ


def test_the_opt_in_mounts_nothing() -> None:
    plain = AgentLoopback(_settings(insecure_localhost_dangerous=True))
    assert plain.mounts() == {}
    assert plain.origin(8100) == "http://localhost:8100"


def test_a_real_handshake_over_the_default_endpoint_completes() -> None:
    a, b = aitp.AitpAgent.generate(), aitp.AitpAgent.generate()
    _manifest(a, HTTPS_LOOPBACK)
    peer = _manifest(b, HTTPS_LOOPBACK)
    session = a.new_session(pinned_keys=[b.aid])
    responder = b.new_responder(pinned_keys=[a.aid])
    ack, sid = responder.process_hello(session.build_hello(peer, [CAP]))
    commit_ack, _ = responder.process_commit(
        session.process_hello_ack(ack, sid), revoked_jtis=set()
    )
    assert session.complete(commit_ack, revoked_jtis=set())
