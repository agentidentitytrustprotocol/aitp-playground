"""Generic Python host adapter shared by every framework."""
from __future__ import annotations

import os

from ...config import Settings
from ...registry.models import AgentManifest
from .base import AgentFramework, AgentHostAdapter, ManifestValidation, PreparedLaunch


class PythonAgentAdapter(AgentHostAdapter):
    """Launches a Python-based agent worker.

    All four supported frameworks (crewai, langchain, langgraph, custom) share
    the exact same launch shape — entrypoint + bootstrap env + cwd. The only
    framework-specific behavior is whether `validate()` enforces a strict
    framework match in the manifest.
    """

    def __init__(self, framework: AgentFramework, *, strict: bool = True) -> None:
        self.framework = framework
        self._strict = strict

    def validate(self, manifest: AgentManifest) -> ManifestValidation:
        errors: list[str] = []
        if self._strict and manifest.metadata.framework != self.framework:
            errors.append(
                f"expected {self.framework}, got {manifest.metadata.framework}"
            )
        ep = manifest.spec.entrypoint
        if not ep.get("value"):
            errors.append("entrypoint.value is required")
        if ep.get("type") not in ("python_module", "python_file"):
            errors.append("entrypoint.type must be python_module or python_file")
        return ManifestValidation(valid=not errors, errors=errors)

    def prepare_launch(
        self,
        manifest: AgentManifest,
        bootstrap_file: str,
        port: int,
        config: Settings,
    ) -> PreparedLaunch:
        host = manifest.spec.host
        return PreparedLaunch(
            command=self._resolve_python(host, config),
            args=self._entrypoint_args(manifest),
            env=self._build_env(manifest, bootstrap_file, port),
            cwd=self._resolve_cwd(manifest, config),
            startup_timeout_ms=host.get("startupTimeoutMs", 30_000),
        )

    @staticmethod
    def _resolve_python(host: dict, config: Settings) -> str:
        """Pick the python interpreter to launch the agent.

        Manifests may pin an absolute path (e.g. a host-side venv) so that
        local `uv run` workflows pick up the right env. In a packaged
        container that absolute path won't exist; fall back to
        `config.agent_python` so the same manifest works inside Docker.
        """
        pinned = host.get("python")
        if pinned and os.path.isabs(pinned) and not os.path.exists(pinned):
            return config.agent_python
        return pinned or config.agent_python
