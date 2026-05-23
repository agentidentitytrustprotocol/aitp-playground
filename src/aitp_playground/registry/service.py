"""Public registry API — index + lookup with optional caching."""
from __future__ import annotations

import time
from typing import Iterable

from ..config import Settings
from ..errors import AgentManifestNotFoundError, ScenarioNotFoundError
from .loader import FileRegistryLoader, LoadedRegistry
from .models import AgentManifest, Pack, ScenarioVersion


class RegistryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loader = FileRegistryLoader(settings.scenarios_path)
        self._cached: LoadedRegistry | None = None
        self._loaded_at_ms: float = 0.0

    def _maybe_reload(self) -> LoadedRegistry:
        ttl = self.settings.registry_cache_ttl_ms
        now = time.monotonic() * 1000
        if (
            self._cached is None
            or ttl == 0
            or (now - self._loaded_at_ms) >= ttl
        ):
            self._cached = self._loader.load()
            self._loaded_at_ms = now
        return self._cached

    def list_packs(self) -> list[Pack]:
        return list(self._maybe_reload().packs.values())

    def list_scenarios(self) -> list[ScenarioVersion]:
        return list(self._maybe_reload().scenarios.values())

    def get_scenario(self, ref: str) -> ScenarioVersion:
        """ref is 'pack/scenario@version'."""
        reg = self._maybe_reload()
        sv = reg.scenarios.get(ref)
        if sv is None:
            raise ScenarioNotFoundError(f"scenario not found: {ref}")
        return sv

    def get_agent_manifest(self, ref: str) -> AgentManifest:
        """ref is a path relative to scenarios_dir (no .yaml suffix), e.g. '_shared/agents/researcher'."""
        reg = self._maybe_reload()
        manifest = reg.agents.get(ref)
        if manifest is not None:
            return manifest
        # Fall back to direct file load (refs may live outside _shared/)
        try:
            return self._loader.load_agent_by_ref(ref)
        except Exception as exc:  # noqa: BLE001
            raise AgentManifestNotFoundError(f"agent manifest not found: {ref}") from exc

    def all_agent_refs(self) -> Iterable[str]:
        return self._maybe_reload().agents.keys()
