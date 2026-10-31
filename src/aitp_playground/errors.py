"""Error types and FastAPI exception handlers."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class PlaygroundError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ScenarioNotFoundError(PlaygroundError):
    status_code = 404
    code = "scenario_not_found"


class AgentManifestNotFoundError(PlaygroundError):
    status_code = 404
    code = "agent_manifest_not_found"


class RegistryValidationError(PlaygroundError):
    status_code = 422
    code = "registry_validation_error"


class RunNotFoundError(PlaygroundError):
    status_code = 404
    code = "run_not_found"


class AgentSpawnError(PlaygroundError):
    status_code = 500
    code = "agent_spawn_error"


class SdkFeatureUnavailableError(PlaygroundError):
    """A scenario needs an optional SDK feature the installed ``aitp-sdk``
    lacks. Raised before any agent is spawned."""

    status_code = 409
    code = "sdk_feature_unavailable"

    def __init__(self, scenario_ref: str, missing: list[str], sdk_version: str | None) -> None:
        self.scenario_ref = scenario_ref
        self.missing = list(missing)
        super().__init__(
            f"{self.code}: {scenario_ref} needs {', '.join(self.missing)}; "
            f"the installed aitp-sdk {sdk_version or '(none)'} lacks it"
        )


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlaygroundError)
    async def handle_playground_error(_: Request, exc: PlaygroundError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
