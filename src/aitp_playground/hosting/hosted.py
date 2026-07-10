"""HostedAgentManager: bring up a single long-lived agent addressable at this
service's *public* origin.

This is the federation primitive behind the cross-domain (Level 1/2) demos.
A normal scenario run spawns all its agents locally and tears them down; a
federated run needs org-B to keep one agent (the analyzer) alive at its own
origin so org-A can resolve it via did:web and handshake across the boundary.

The manager owns nothing AITP-specific — it reuses the same
PortAllocator / BootstrapBuilder / adapter / AgentSupervisor the runner uses.
The hosted agent still performs its own handshake via the SDK; the playground
never speaks the protocol on its behalf.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional
from urllib.parse import quote

from ..config import Settings
from ..registry.models import AgentSpec
from ..registry.service import RegistryService
from .adapters.registry import AdapterRegistry
from .bootstrap import BootstrapBuilder
from .port_allocator import PortAllocator
from .supervisor import AgentSupervisor

logger = logging.getLogger(__name__)


@dataclass
class HostedAgent:
    hosted_id: str
    agent_id: str
    ref: str
    port: int
    aid: str
    did: Optional[str]
    origin: str
    manifest_url: str
    handshake_url: str
    did_document_url: Optional[str]


class HostedAgentManager:
    def __init__(
        self,
        *,
        registry: RegistryService,
        bootstrap_builder: BootstrapBuilder,
        adapters: AdapterRegistry,
        supervisor: AgentSupervisor,
        settings: Settings,
        port_alloc: Optional[PortAllocator] = None,
    ) -> None:
        self.registry = registry
        self.bootstrap_builder = bootstrap_builder
        self.adapters = adapters
        self.supervisor = supervisor
        self.settings = settings
        # Dedicated allocator well clear of the runner's range so hosted,
        # long-lived agents never collide with ephemeral scenario agents.
        self.port_alloc = port_alloc or PortAllocator(start=settings.agent_base_port + 1000)
        self._hosted: dict[str, HostedAgent] = {}

    def _run_id(self, hosted_id: str) -> str:
        return f"hosted-{hosted_id}"

    async def host(
        self,
        *,
        ref: str,
        public_host: Optional[str] = None,
        public_scheme: Optional[str] = None,
        signing_suite: Optional[str] = None,
        inputs: Optional[dict[str, Any]] = None,
        port: Optional[int] = None,
    ) -> HostedAgent:
        """Spawn one agent from ``ref`` and keep it alive at this service's
        public origin. Falls back to the configured PUBLIC_HOST/PUBLIC_SCHEME
        when the caller does not override them. ``port`` pins the container
        listen port so it can match an advertised (proxied or literal) public
        origin; when omitted the dedicated allocator picks one."""
        host = public_host or self.settings.public_host
        scheme = public_scheme or self.settings.public_scheme
        if not host:
            raise ValueError(
                "no public host configured — set PUBLIC_HOST or pass public_host"
            )

        hosted_id = uuid.uuid4().hex[:12]
        run_id = self._run_id(hosted_id)
        resolved_manifest = self.registry.get_agent_manifest(ref)
        # Stable, path-safe id (matches how scenarios name agents), not the
        # human display name — it keys seed derivation, telemetry, and the
        # supervisor process table.
        agent_id = ref.rsplit("/", 1)[-1]

        origin = f"{scheme}://{host}".rstrip("/")
        # did:web encodes the port (if any) as %3A; a bare host (standard 443)
        # yields a clean did:web:org-b.aitp.test.
        did = f"did:web:{quote(host, safe='.')}"

        agent_spec = AgentSpec(
            id=agent_id,
            ref=ref,
            org="external",
            did_web_host=host,
            signing_suite=signing_suite,  # type: ignore[arg-type]
        )

        port = port if port is not None else self.port_alloc.allocate()
        try:
            bs = self.bootstrap_builder.build(
                run_id=run_id,
                agent_spec=agent_spec,
                resolved_manifest=resolved_manifest,
                port=port,
                peers={},
                inputs=inputs or {},
                public_origin=origin,
            )
            bootstrap_file = self.bootstrap_builder.write(bs)

            adapter = self.adapters.get(resolved_manifest.metadata.framework)
            validation = adapter.validate(resolved_manifest)
            if not validation.valid:
                raise ValueError(f"manifest invalid for {ref}: {validation.errors}")
            prepared = adapter.prepare_launch(
                resolved_manifest, bootstrap_file, port, self.settings
            )
            ra = await self.supervisor.launch(
                run_id=run_id,
                agent_id=agent_id,
                prepared=prepared,
                port=port,
                startup_timeout_ms=int(
                    resolved_manifest.spec.host.get("startupTimeoutMs", 30_000)
                ),
            )
        except Exception:
            self.port_alloc.release(port)
            raise

        hosted = HostedAgent(
            hosted_id=hosted_id,
            agent_id=agent_id,
            ref=ref,
            port=port,
            aid=ra.aid,
            did=did,
            origin=origin,
            manifest_url=f"{origin}/.well-known/aitp-manifest",
            handshake_url=f"{origin}/aitp/handshake/hello",
            did_document_url=f"{origin}/.well-known/did.json",
        )
        self._hosted[hosted_id] = hosted
        logger.info(
            "Hosted agent %s (%s) at %s aid=%s local_port=%d",
            agent_id, ref, origin, ra.aid, port,
        )
        return hosted

    def get(self, hosted_id: str) -> Optional[HostedAgent]:
        return self._hosted.get(hosted_id)

    def local_port(self, hosted_id: str) -> Optional[int]:
        h = self._hosted.get(hosted_id)
        return h.port if h else None

    def list(self) -> list[dict[str, Any]]:
        return [asdict(h) for h in self._hosted.values()]

    def stop(self, hosted_id: str) -> bool:
        hosted = self._hosted.pop(hosted_id, None)
        if hosted is None:
            return False
        self.supervisor.kill(self._run_id(hosted_id), hosted.agent_id)
        self.port_alloc.release(hosted.port)
        return True

    def stop_all(self) -> None:
        for hosted_id in list(self._hosted):
            self.stop(hosted_id)
