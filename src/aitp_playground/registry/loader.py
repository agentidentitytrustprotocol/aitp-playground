"""Filesystem-backed scenario pack loader."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..errors import RegistryValidationError
from .include_resolver import load_yaml
from .models import AgentManifest, Pack, ScenarioTemplate, ScenarioVersion

logger = logging.getLogger(__name__)


@dataclass
class LoadedRegistry:
    packs: dict[str, Pack] = field(default_factory=dict)
    # key: "pack/scenario@version"
    scenarios: dict[str, ScenarioVersion] = field(default_factory=dict)
    # key: ref relative to scenarios_dir without trailing .yaml (e.g. "_shared/agents/researcher")
    agents: dict[str, AgentManifest] = field(default_factory=dict)
    # key: "pack/scenario@version" -> {template_name: ScenarioTemplate}
    templates: dict[str, dict[str, ScenarioTemplate]] = field(default_factory=dict)


class FileRegistryLoader:
    """Walks scenarios_dir and produces a LoadedRegistry."""

    def __init__(self, scenarios_dir: Path) -> None:
        self.scenarios_dir = Path(scenarios_dir).resolve()

    def load(self) -> LoadedRegistry:
        if not self.scenarios_dir.exists():
            raise RegistryValidationError(
                f"scenarios_dir does not exist: {self.scenarios_dir}"
            )

        reg = LoadedRegistry()

        # 1. Load every shared agent manifest under <root>/_shared/agents/*.yaml
        shared_agents_dir = self.scenarios_dir / "_shared" / "agents"
        if shared_agents_dir.exists():
            for p in sorted(shared_agents_dir.glob("*.yaml")):
                ref = self._ref_for_agent_file(p)
                reg.agents[ref] = self._load_agent_manifest(p)

        # 2. For each pack: read pack.yaml + all scenario versions
        for pack_dir in sorted(self.scenarios_dir.iterdir()):
            if not pack_dir.is_dir() or pack_dir.name.startswith("_"):
                continue
            pack_yaml = pack_dir / "pack.yaml"
            if not pack_yaml.exists():
                logger.warning("Pack dir missing pack.yaml: %s", pack_dir)
                continue
            pack = self._load_pack(pack_yaml)
            reg.packs[pack.metadata.slug] = pack

            for scenario_dir in sorted(p for p in pack_dir.iterdir() if p.is_dir()):
                for version_dir in sorted(p for p in scenario_dir.iterdir() if p.is_dir()):
                    sv_yaml = version_dir / "scenario.yaml"
                    if not sv_yaml.exists():
                        continue
                    sv = self._load_scenario_version(sv_yaml)
                    key = f"{sv.metadata.pack}/{sv.metadata.scenario}@{sv.metadata.version}"
                    reg.scenarios[key] = sv
                    templates = self._load_templates(version_dir)
                    if templates:
                        reg.templates[key] = templates

        return reg

    def _load_templates(self, version_dir: Path) -> dict[str, ScenarioTemplate]:
        templates_dir = version_dir / "templates"
        if not templates_dir.exists():
            return {}
        out: dict[str, ScenarioTemplate] = {}
        for p in sorted(templates_dir.glob("*.yaml")):
            data = load_yaml(p)
            if not isinstance(data, dict) or data.get("kind") != "ScenarioTemplate":
                logger.warning(
                    "skipping non-template file under templates/: %s "
                    "(missing kind: ScenarioTemplate)", p,
                )
                continue
            try:
                tpl = ScenarioTemplate.model_validate(data)
            except ValidationError as exc:
                raise RegistryValidationError(f"{p}: {exc}") from exc
            if tpl.metadata.name in out:
                raise RegistryValidationError(
                    f"{p}: duplicate template name {tpl.metadata.name!r}"
                )
            out[tpl.metadata.name] = tpl
        return out

    def _ref_for_agent_file(self, path: Path) -> str:
        rel = path.relative_to(self.scenarios_dir).with_suffix("")
        return str(rel).replace("\\", "/")

    def _load_pack(self, path: Path) -> Pack:
        data = load_yaml(path)
        try:
            return Pack.model_validate(data)
        except ValidationError as exc:
            raise RegistryValidationError(f"{path}: {exc}") from exc

    def _load_scenario_version(self, path: Path) -> ScenarioVersion:
        data = load_yaml(path)
        try:
            return ScenarioVersion.model_validate(data)
        except ValidationError as exc:
            raise RegistryValidationError(f"{path}: {exc}") from exc

    def _load_agent_manifest(self, path: Path) -> AgentManifest:
        data = load_yaml(path)
        try:
            return AgentManifest.model_validate(data)
        except ValidationError as exc:
            raise RegistryValidationError(f"{path}: {exc}") from exc

    def load_agent_by_ref(self, ref: str) -> AgentManifest:
        """Resolve a single agent ref. Caller passes refs like '_shared/agents/researcher'."""
        rel = ref if ref.endswith(".yaml") else f"{ref}.yaml"
        target = (self.scenarios_dir / rel).resolve()
        if not target.exists():
            raise RegistryValidationError(f"agent manifest not found for ref: {ref}")
        return self._load_agent_manifest(target)
