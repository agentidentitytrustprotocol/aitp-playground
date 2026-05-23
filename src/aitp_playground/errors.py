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


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlaygroundError)
    async def handle_playground_error(_: Request, exc: PlaygroundError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
