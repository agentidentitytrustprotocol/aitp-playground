"""Unit tests for ``HostedAgentManager.host()`` (hosting/hosted.py).

Mirrors the house pattern from tests/unit/test_engine_run.py: a fake
supervisor that never spawns a real subprocess and fake adapters that skip
the real python_agent validation, wired against the *real*
``RegistryService`` / ``BootstrapBuilder`` / ``PortAllocator`` so the tests
observe the manager's actual did:web / origin assembly and port-release
side effects — not a mocked-out version of them.

The full spawn-and-handshake path (a real subprocess, a real handshake
across two ports) lives in tests/integration/test_federated_handshake.py
(gated on AITP_E2E); this file is the fast, always-on unit layer for the
branching inside ``host()`` itself.
"""
from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from aitp_playground.config import Settings
from aitp_playground.hosting.adapters.base import ManifestValidation
from aitp_playground.hosting.bootstrap import BootstrapBuilder
from aitp_playground.hosting.hosted import HostedAgentManager
from aitp_playground.hosting.port_allocator import PortAllocator
from aitp_playground.hosting.supervisor import RunningAgent
from aitp_playground.registry.service import RegistryService

ANALYZER_REF = "_shared/agents/analyzer"


# --------------------------------------------------------------------------- #
# fakes (same shape as test_engine_run.py's FakeAdapter(s)/FakeSupervisor)
# --------------------------------------------------------------------------- #


class FakeAdapter:
    def __init__(self, errors: Optional[list[str]] = None) -> None:
        self.errors = list(errors or [])
        self.validated: list[Any] = []
        self.prepared: list[tuple[str, int]] = []

    def validate(self, manifest: Any) -> ManifestValidation:
        self.validated.append(manifest)
        return ManifestValidation(valid=not self.errors, errors=self.errors)

    def prepare_launch(self, manifest: Any, bootstrap_file: str, port: int, config: Settings) -> Any:
        self.prepared.append((bootstrap_file, port))
        return SimpleNamespace(command="noop", args=[], env={}, cwd=".")


class FakeAdapters:
    def __init__(self, errors: Optional[list[str]] = None) -> None:
        self.adapter = FakeAdapter(errors)

    def get(self, framework: str) -> FakeAdapter:
        return self.adapter


class FakeSupervisor:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.launched: list[str] = []
        self.killed: list[tuple[str, str]] = []

    async def launch(
        self, *, run_id: str, agent_id: str, prepared: Any, port: int,
        startup_timeout_ms: int = 30_000,
    ) -> RunningAgent:
        self.launched.append(agent_id)
        if self.fail:
            raise RuntimeError("subprocess failed to signal ready")
        return RunningAgent(
            run_id=run_id, agent_id=agent_id, port=port, pid=4242,
            aid=f"aid-{agent_id}",
            manifest_url=f"http://localhost:{port}/.well-known/aitp-manifest",
            status="ready",
        )

    def kill(self, run_id: str, agent_id: str) -> None:
        self.killed.append((run_id, agent_id))


def _settings(**overrides: Any) -> Settings:
    return Settings(cp_base_url="", cp_api_key="", **overrides)


def _manager(
    *,
    settings: Optional[Settings] = None,
    adapters: Optional[FakeAdapters] = None,
    supervisor: Optional[FakeSupervisor] = None,
    port_alloc: Optional[PortAllocator] = None,
) -> tuple[HostedAgentManager, FakeSupervisor, PortAllocator]:
    settings = settings or _settings(public_host="org-b.aitp.test", public_scheme="https")
    supervisor = supervisor or FakeSupervisor()
    port_alloc = port_alloc or PortAllocator(start=20000)
    mgr = HostedAgentManager(
        registry=RegistryService(settings),
        bootstrap_builder=BootstrapBuilder(settings),
        adapters=adapters or FakeAdapters(),
        supervisor=supervisor,
        settings=settings,
        port_alloc=port_alloc,
    )
    return mgr, supervisor, port_alloc


# --------------------------------------------------------------------------- #
# host() — success path / did:web + origin assembly
# --------------------------------------------------------------------------- #


async def test_host_uses_configured_public_host_and_scheme() -> None:
    mgr, supervisor, ports = _manager()
    hosted = await mgr.host(ref=ANALYZER_REF)

    assert hosted.agent_id == "analyzer"
    assert hosted.ref == ANALYZER_REF
    assert hosted.port == 20000
    assert hosted.origin == "https://org-b.aitp.test"
    assert hosted.did == "did:web:org-b.aitp.test"
    assert hosted.manifest_url == "https://org-b.aitp.test/.well-known/aitp-manifest"
    assert hosted.handshake_url == "https://org-b.aitp.test/aitp/handshake/hello"
    assert hosted.did_document_url == "https://org-b.aitp.test/.well-known/did.json"
    assert hosted.aid == "aid-analyzer"
    assert supervisor.launched == ["analyzer"]

    # Registered for later lookup.
    assert mgr.get(hosted.hosted_id) is hosted
    assert mgr.local_port(hosted.hosted_id) == 20000
    assert mgr.list() == [asdict(hosted)]


async def test_host_did_web_encodes_a_colon_in_the_host_as_percent_3a() -> None:
    """A public host that embeds a port (proxied or literal) must produce a
    did:web whose colon is percent-encoded — a bare did:web:host:port would
    be ambiguous with the did:web path-segment convention."""
    mgr, _supervisor, _ports = _manager(
        settings=_settings(public_host="org-b.aitp.test:9100", public_scheme="http"),
    )
    hosted = await mgr.host(ref=ANALYZER_REF)
    assert hosted.did == "did:web:org-b.aitp.test%3A9100"
    assert hosted.origin == "http://org-b.aitp.test:9100"


async def test_host_call_override_wins_over_settings_default() -> None:
    mgr, _supervisor, _ports = _manager(
        settings=_settings(public_host="settings-host.test", public_scheme="http"),
    )
    hosted = await mgr.host(
        ref=ANALYZER_REF, public_host="override.test", public_scheme="https",
    )
    assert hosted.origin == "https://override.test"
    assert hosted.did == "did:web:override.test"


async def test_host_explicit_port_pins_the_container_port_without_consuming_the_allocator() -> None:
    mgr, _supervisor, ports = _manager()
    hosted = await mgr.host(ref=ANALYZER_REF, port=12345)
    assert hosted.port == 12345
    # The dedicated allocator was never touched by the explicit-port path, so
    # its own sequence still starts at its configured base.
    assert ports.allocate() == 20000


# --------------------------------------------------------------------------- #
# host() — failure paths
# --------------------------------------------------------------------------- #


async def test_host_raises_when_no_public_host_is_configured_anywhere() -> None:
    mgr, supervisor, ports = _manager(settings=_settings())  # public_host="" default
    with pytest.raises(ValueError, match="no public host configured"):
        await mgr.host(ref=ANALYZER_REF)
    # Failed before any port/agent bookkeeping happened.
    assert supervisor.launched == []
    assert mgr.list() == []
    assert ports.allocate() == 20000  # allocator sequence untouched


async def test_host_adapter_validation_failure_releases_the_port_and_raises() -> None:
    mgr, supervisor, ports = _manager(adapters=FakeAdapters(errors=["missing entrypoint"]))
    with pytest.raises(ValueError, match=f"manifest invalid for {ANALYZER_REF}"):
        await mgr.host(ref=ANALYZER_REF)
    # Never reached the supervisor, and the allocated port went back to the pool.
    assert supervisor.launched == []
    assert mgr.list() == []
    assert ports.allocate() == 20000


async def test_host_supervisor_launch_failure_releases_the_port_and_propagates() -> None:
    mgr, supervisor, ports = _manager(supervisor=FakeSupervisor(fail=True))
    with pytest.raises(RuntimeError, match="subprocess failed to signal ready"):
        await mgr.host(ref=ANALYZER_REF)
    assert supervisor.launched == ["analyzer"]
    assert mgr.list() == []
    assert ports.allocate() == 20000


# --------------------------------------------------------------------------- #
# get / local_port / list / stop / stop_all
# --------------------------------------------------------------------------- #


async def test_get_and_local_port_return_none_for_unknown_id() -> None:
    mgr, _supervisor, _ports = _manager()
    assert mgr.get("no-such-id") is None
    assert mgr.local_port("no-such-id") is None


def test_stop_returns_false_for_unknown_id() -> None:
    mgr, _supervisor, _ports = _manager()
    assert mgr.stop("no-such-id") is False


async def test_stop_kills_the_agent_releases_the_port_and_forgets_it() -> None:
    mgr, supervisor, ports = _manager()
    hosted = await mgr.host(ref=ANALYZER_REF)

    assert mgr.stop(hosted.hosted_id) is True
    assert supervisor.killed == [(f"hosted-{hosted.hosted_id}", "analyzer")]
    assert mgr.get(hosted.hosted_id) is None
    assert mgr.list() == []
    # Port recycled back to the dedicated allocator.
    assert ports.allocate() == hosted.port
    # Second stop on the same (now-forgotten) id is a no-op.
    assert mgr.stop(hosted.hosted_id) is False


async def test_stop_all_stops_every_hosted_agent() -> None:
    mgr, supervisor, _ports = _manager()
    h1 = await mgr.host(ref=ANALYZER_REF)
    h2 = await mgr.host(ref=ANALYZER_REF)
    assert len(mgr.list()) == 2

    mgr.stop_all()

    assert mgr.list() == []
    assert set(supervisor.killed) == {
        (f"hosted-{h1.hosted_id}", "analyzer"),
        (f"hosted-{h2.hosted_id}", "analyzer"),
    }
