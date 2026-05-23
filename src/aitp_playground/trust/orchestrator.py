"""TrustOrchestrator: resolve peer manifest URLs per scenario discovery mode."""
from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..cp_client.client import CpClient
from ..hosting.supervisor import RunningAgent
from ..registry.models import ScenarioVersion
from .resolver import resolve_did_web

logger = logging.getLogger(__name__)


class TrustOrchestrator:
    def __init__(self, cp: CpClient, settings: Settings) -> None:
        self.cp = cp
        self.settings = settings

    async def resolve_peers(
        self,
        scenario: ScenarioVersion,
        running: dict[str, RunningAgent],
    ) -> dict[str, dict[str, Any]]:
        """Returns {agent_id: {manifest_url, did}}."""
        discovery = scenario.spec.trust.discovery
        peers: dict[str, dict[str, Any]] = {}

        for agent_spec in scenario.spec.agents:
            ra = running.get(agent_spec.id)
            local_manifest = (
                f"http://localhost:{ra.port}/.well-known/aitp-manifest" if ra else None
            )
            local_handshake = (
                f"http://localhost:{ra.port}/aitp/handshake/hello" if ra else None
            )

            if discovery == "did_web" and agent_spec.did_web_host:
                did = f"did:web:{agent_spec.did_web_host.replace(':', '%3A')}"
                try:
                    manifest_url = await resolve_did_web(did)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "did:web resolve failed for %s (%s); falling back to localhost",
                        agent_spec.id, exc,
                    )
                    manifest_url = local_manifest
                peers[agent_spec.id] = {"manifest_url": manifest_url, "did": did}
                continue

            if discovery == "cp_registry" and agent_spec.org == "external":
                cap_hint = self._cap_for_agent(agent_spec.id, scenario)
                discovered = []
                try:
                    discovered = await self.cp.discover_by_capability(cap_hint or "")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CP discovery failed (%s); using static fallback", exc)
                if discovered:
                    handshake = discovered[0].get("handshake_endpoint") or local_handshake
                    base = handshake.rsplit("/aitp", 1)[0] if handshake else None
                    peers[agent_spec.id] = {
                        "manifest_url": f"{base}/.well-known/aitp-manifest" if base else local_manifest,
                        "did": None,
                        "source": "cp_registry",
                    }
                    continue
                logger.warning(
                    "CP discovery found no agents for %s; using static localhost",
                    agent_spec.id,
                )
                peers[agent_spec.id] = {"manifest_url": local_manifest, "did": None, "source": "static_fallback"}
                continue

            peers[agent_spec.id] = {"manifest_url": local_manifest, "did": None, "source": "static"}

        return peers

    @staticmethod
    def _cap_for_agent(agent_id: str, scenario: ScenarioVersion) -> str | None:
        for step in scenario.spec.workflow.steps:
            if step.agent == agent_id and step.capability:
                return step.capability
        return None
