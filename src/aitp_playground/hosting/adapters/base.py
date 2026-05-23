"""Abstract base for framework-specific host adapters."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ...config import Settings
from ...registry.models import AgentManifest

AgentFramework = Literal["crewai", "langchain", "langgraph", "custom"]


@dataclass
class PreparedLaunch:
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    startup_timeout_ms: int = 30_000


@dataclass
class ManifestValidation:
    valid: bool
    errors: list[str] = field(default_factory=list)


class AgentHostAdapter(ABC):
    framework: AgentFramework

    @abstractmethod
    def validate(self, manifest: AgentManifest) -> ManifestValidation: ...

    @abstractmethod
    def prepare_launch(
        self,
        manifest: AgentManifest,
        bootstrap_file: str,
        port: int,
        config: Settings,
    ) -> PreparedLaunch: ...

    # ---- helpers shared by all Python-based adapters ----

    def _resolve_cwd(self, manifest: AgentManifest, config: Settings) -> str:
        host = manifest.spec.host
        cwd = host.get("cwd", ".")
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            # cwd is relative to the project root (parent of scenarios_dir)
            project_root = Path(config.scenarios_dir).resolve().parent
            cwd_path = (project_root / cwd_path).resolve()
        return str(cwd_path)

    def _build_env(self, manifest: AgentManifest, bootstrap_file: str, port: int) -> dict[str, str]:
        host = manifest.spec.host
        env: dict[str, str] = {
            **{k: v for k, v in os.environ.items() if isinstance(v, str)},
            **{k: str(v) for k, v in (host.get("env") or {}).items()},
            "AITP_BOOTSTRAP_FILE": bootstrap_file,
            "AGENT_PORT": str(port),
            "PYTHONUNBUFFERED": "1",
        }
        # Make sure agents can `from bootstrap import ...` and `from aitp_server import ...`
        project_root = Path(__file__).resolve().parents[4]
        base_module = project_root / "agents" / "base"
        existing_pp = env.get("PYTHONPATH", "")
        parts = [str(base_module), str(project_root / "agents")]
        if existing_pp:
            parts.append(existing_pp)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    @staticmethod
    def _entrypoint_args(manifest: AgentManifest) -> list[str]:
        ep = manifest.spec.entrypoint
        kind = ep.get("type")
        value = ep.get("value")
        if kind == "python_module":
            return ["-m", value]
        if kind == "python_file":
            return [value]
        raise ValueError(f"unsupported entrypoint.type: {kind}")
