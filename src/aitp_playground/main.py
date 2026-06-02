"""FastAPI app factory + dependency wiring."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import agents as agents_api
from .api import cp as cp_api
from .api import dashboard as dashboard_api
from .api import health as health_api
from .api import metrics as metrics_api
from .api import registry as registry_api
from .api import runs as runs_api
from .api import telemetry as telemetry_api
from .api import webhooks as webhooks_api
from .config import Settings, get_settings
from .cp_client.client import CpClient
from .errors import install_handlers
from .hosting.adapters.registry import build_default_adapter_registry
from .hosting.bootstrap import BootstrapBuilder
from .hosting.port_allocator import PortAllocator
from .hosting.supervisor import AgentSupervisor
from .registry.service import RegistryService
from .runner.engine import ScenarioRunner
from .runner.store import RunStore, build_run_store
from .trust.orchestrator import TrustOrchestrator


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    registry = RegistryService(settings)
    port_alloc = PortAllocator(start=settings.agent_base_port)
    supervisor = AgentSupervisor()
    bootstrap_builder = BootstrapBuilder(settings)
    adapters = build_default_adapter_registry()
    cp = CpClient(settings)
    trust = TrustOrchestrator(cp, settings)
    run_store = build_run_store(settings.run_history_db or None)
    runner = ScenarioRunner(
        registry=registry,
        supervisor=supervisor,
        bootstrap_builder=bootstrap_builder,
        adapters=adapters,
        trust=trust,
        cp=cp,
        port_alloc=port_alloc,
        config=settings,
        store=run_store,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Best-effort cleanup of any agent processes still alive
        for run_id in list(run_store.list_ids()):
            supervisor.kill_run(run_id)

    app = FastAPI(title="aitp-playground", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.registry = registry
    app.state.runner = runner
    app.state.run_store = run_store
    app.state.supervisor = supervisor
    app.state.cp = cp

    install_handlers(app)

    app.include_router(health_api.router)
    app.include_router(registry_api.router)
    app.include_router(runs_api.router)
    app.include_router(agents_api.router)
    app.include_router(telemetry_api.router)
    app.include_router(metrics_api.router)
    app.include_router(webhooks_api.router)
    app.include_router(cp_api.router)
    app.include_router(dashboard_api.router)

    return app


app = create_app()
