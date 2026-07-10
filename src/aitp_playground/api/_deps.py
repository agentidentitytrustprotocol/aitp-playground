"""FastAPI dependency helpers — pull services from app.state."""
from __future__ import annotations

from fastapi import Request

from ..cp_client.client import CpClient
from ..registry.service import RegistryService
from ..runner.engine import ScenarioRunner
from ..runner.store import RunStore
from ..hosting.supervisor import AgentSupervisor


def get_registry(request: Request) -> RegistryService:
    return request.app.state.registry


def get_runner(request: Request) -> ScenarioRunner:
    return request.app.state.runner


def get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store


def get_supervisor(request: Request) -> AgentSupervisor:
    return request.app.state.supervisor


def get_cp_client(request: Request) -> CpClient:
    return request.app.state.cp


def get_hosted_manager(request: Request):
    return request.app.state.hosted
